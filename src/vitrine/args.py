"""Declarative command-argument parsing.

Any handler parameter that isn't framework-supplied or provider-registered is
treated as a command argument, converted by its annotation::

    @router.command("pay")
    async def pay(user, amount: float, note: Greedy = ""):
        ...

``/pay 5 thanks for lunch`` -> ``amount=5.0, note="thanks for lunch"``.
Required, optional (has a default), and greedy-trailing (:class:`Greedy`,
consumes the rest of the line) parameters are supported. Arity mismatches and
conversion failures raise :class:`UsageError` with an auto-generated usage
line, rendered as friendly UX by the error layer.
"""

from __future__ import annotations

import inspect
from collections.abc import Set
from dataclasses import dataclass
from typing import Any, Callable, get_args, get_origin

from .exceptions import ConfigurationError, UsageError


class Greedy(str):
    """Annotation for a trailing parameter that consumes the rest of the message."""


_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}


def _convert(raw: str, annotation: Any, name: str) -> Any:
    if annotation in (inspect.Parameter.empty, str, Greedy, Any):
        return raw

    if annotation is bool:
        lowered = raw.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise UsageError("", hint=f"{name} must be yes/no")

    if get_origin(annotation) is not None:  # Optional[int] etc: try each arg
        for candidate in get_args(annotation):
            if candidate is type(None):
                continue
            try:
                return _convert(raw, candidate, name)
            except UsageError:
                continue
        raise UsageError("", hint=f"could not parse {name}={raw!r}")

    try:
        return annotation(raw)
    except (ValueError, TypeError) as exc:
        labels = {int: "integer", float: "number"}
        kind = labels.get(annotation, getattr(annotation, "__name__", str(annotation)))
        raise UsageError(
            "", hint=f"{name} must be a valid {kind} (got {raw!r})"
        ) from exc


@dataclass(frozen=True)
class ArgSpec:
    name: str
    annotation: Any
    required: bool
    greedy: bool
    default: Any = None

    def placeholder(self) -> str:
        inner = f"{self.name}..." if self.greedy else self.name
        return f"<{inner}>" if self.required else f"[{inner}]"


def build_arg_specs(fn: Callable[..., Any], skip: Set[str]) -> list[ArgSpec]:
    """Derive arg specs from ``fn``'s signature, ignoring injected names."""
    try:
        signature = inspect.signature(fn, eval_str=True)  # PEP 563 strings -> types
    except NameError:
        signature = inspect.signature(fn)

    specs: list[ArgSpec] = []
    for param in signature.parameters.values():
        if param.name in skip or param.kind in (
            param.VAR_POSITIONAL,
            param.VAR_KEYWORD,
        ):
            continue
        greedy = param.annotation is Greedy
        required = param.default is param.empty
        if specs and specs[-1].greedy:
            raise ConfigurationError(
                f"{fn.__name__}: greedy parameter {specs[-1].name!r} must come last"
            )
        specs.append(
            ArgSpec(
                name=param.name,
                annotation=param.annotation,
                required=required,
                greedy=greedy,
                default=None if required else param.default,
            )
        )

    return specs


def usage_string(command: str, specs: list[ArgSpec]) -> str:
    parts = [f"/{command}", *(spec.placeholder() for spec in specs)]
    return " ".join(parts)


def parse_args(command: str, specs: list[ArgSpec], text: str | None) -> dict[str, Any]:
    """Parse the text after the command into named, typed values."""
    usage = usage_string(command, specs)
    remainder = (text or "").strip()
    values: dict[str, Any] = {}
    for spec in specs:
        if spec.greedy:
            if not remainder and spec.required:
                raise UsageError(usage, hint=f"missing {spec.name}")
            values[spec.name] = remainder if remainder else spec.default
            remainder = ""
            continue

        if not remainder:
            if spec.required:
                raise UsageError(usage, hint=f"missing {spec.name}")
            values[spec.name] = spec.default
            continue

        token, _, remainder = remainder.partition(" ")
        remainder = remainder.strip()
        try:
            values[spec.name] = _convert(token, spec.annotation, spec.name)
        except UsageError as exc:
            raise UsageError(usage, hint=exc.hint) from None

    if remainder:
        raise UsageError(usage, hint="too many arguments")

    return values
