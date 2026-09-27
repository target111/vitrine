"""Declarative command-argument parsing.

Command arguments are declared explicitly, by annotation::

    @router.command("pay")
    async def pay(user, amount: Arg[float], note: Greedy = ""):
        ...

``/pay 5 thanks for lunch`` -> ``amount=5.0, note="thanks for lunch"``.
``Arg[T]`` is an argument converted by calling ``T``; :class:`Greedy` is a
trailing text argument that consumes the rest of the line. Every other
parameter is injected, so a misspelled service name is a build error rather
than a surprise argument. Required, optional (has a default) and greedy
arguments are supported; arity mismatches and conversion failures raise
:class:`UsageError` with an auto-generated usage line.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, TypeAlias, TypeVar, get_args, get_origin

from .exceptions import ConfigurationError, UsageError
from .injection import _resolve_lax

T = TypeVar("T")


class _ArgMarker:
    def __repr__(self) -> str:
        return "Arg"


_ARG = _ArgMarker()

#: ``amount: Arg[float]`` marks a command argument; type checkers see ``float``
Arg: TypeAlias = Annotated[T, _ARG]


class Greedy(str):
    """Annotation for a trailing argument that consumes the rest of the message."""


_TRUE = {"1", "true", "yes", "y", "on"}
_FALSE = {"0", "false", "no", "n", "off"}

_TYPE_LABELS: dict[Any, str] = {int: "integer", float: "number", bool: "yes/no"}

#: how a PEP 563 annotation string names an argument, for when it can't be evaluated
_ARG_SPELLING = re.compile(r"(^|[.\s\[])(Arg\[|Greedy\b)")


def type_label(annotation: Any) -> str:
    """What to call an argument's type when talking to the user."""
    if annotation in (inspect.Parameter.empty, str, Greedy, Any):
        return "text"

    return _TYPE_LABELS.get(annotation) or str(getattr(annotation, "__name__", annotation))


def _convert(raw: str, annotation: Any, name: str) -> Any:
    if annotation in (inspect.Parameter.empty, str, Greedy, Any):
        return raw

    if annotation is bool:
        lowered = raw.lower()
        if lowered in _TRUE:
            return True
        if lowered in _FALSE:
            return False
        raise UsageError("", hint=f"{name} must be {type_label(bool)}")

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
        raise UsageError(
            "", hint=f"{name} must be a valid {type_label(annotation)} (got {raw!r})"
        ) from exc


def argument_type(annotation: Any) -> Any:
    """The value type if ``annotation`` declares a command argument, else ``None``.

    ``Arg[int]`` -> ``int``; ``Greedy`` (bare or as ``Arg[Greedy]``) -> ``Greedy``.
    """
    if annotation is Greedy:
        return Greedy
    if get_origin(annotation) is Annotated:
        inner, *metadata = get_args(annotation)
        if any(item is _ARG for item in metadata):
            return inner
    return None


def _evaluate(fn: Callable[..., Any], param: inspect.Parameter) -> Any:
    """``param``'s annotation as an object; ``None`` if it cannot be evaluated.

    Under PEP 563 annotations are strings. A string that cannot be evaluated
    is almost always a TYPE_CHECKING-only import on an injected parameter,
    which is fine -- unless it is spelled as an argument, whose type has to
    exist at runtime to convert anything.
    """
    annotation = param.annotation
    value = _resolve_lax(annotation, fn)
    if value is None and isinstance(annotation, str) and _ARG_SPELLING.search(annotation):
        raise ConfigurationError(
            f"{fn.__name__}: cannot resolve the annotation {annotation!r} of command "
            f"argument {param.name!r}. An argument is converted by calling its "
            f"type, so it has to be importable at runtime -- not only under "
            f"TYPE_CHECKING."
        )
    return value


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


def build_arg_specs(fn: Callable[..., Any]) -> list[ArgSpec]:
    """The command arguments ``fn`` declares, in order."""
    specs: list[ArgSpec] = []
    for param in inspect.signature(fn).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        value_type = argument_type(_evaluate(fn, param))
        if value_type is None:
            continue
        if specs and specs[-1].greedy:
            raise ConfigurationError(
                f"{fn.__name__}: greedy parameter {specs[-1].name!r} must come last"
            )
        required = param.default is param.empty
        specs.append(
            ArgSpec(
                name=param.name,
                annotation=value_type,
                required=required,
                greedy=value_type is Greedy,
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
