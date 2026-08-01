"""Dependency injection into handlers.

Handlers stay plain async functions; they declare what they need by parameter
name and the framework supplies it. Three sources, in priority order:

1. explicit ``Depends(factory)`` defaults,
2. per-invocation values (``update``, ``context``, ``bot``, ``data``, ``state``,
   ``event``, ``delivery``, parsed command args, middleware extras, principal),
3. registered providers (``bot.provide("db")(make_db)``).

Provider factories may themselves declare parameters (resolved recursively),
may be sync or async, and may be async *generators* — the part after ``yield``
runs as cleanup after the handler finishes, FastAPI-style::

    @bot.provide("session")
    async def session(db):          # "db" comes from another provider
        async with db.begin() as s:
            yield s

Everything is resolved at most once per invocation and cached.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Callable, Set
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_args, get_origin

from .exceptions import InjectionError

if TYPE_CHECKING:
    from .auth import Auth

#: names the framework supplies without any provider registration
RESERVED_NAMES = frozenset(
    {"update", "context", "bot", "data", "state", "event", "delivery", "error"}
)

_MISSING = object()


class Depends:
    """Explicit dependency marker: ``def handler(db = Depends(make_db))``."""

    def __init__(self, factory: Callable[..., Any]) -> None:
        self.factory = factory


class Providers:
    """A flat, name-keyed provider registry. Deliberately not a container."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., Any]] = {}
        self._values: dict[str, Any] = {}

    def register(self, name: str, factory: Callable[..., Any]) -> None:
        self._factories[name] = factory

    def register_value(self, name: str, value: Any) -> None:
        self._values[name] = value

    def __contains__(self, name: str) -> bool:
        return name in self._factories or name in self._values

    def names(self) -> set[str]:
        return set(self._factories) | set(self._values)

    def value(self, name: str) -> Any:
        return self._values.get(name, _MISSING)

    def factory(self, name: str) -> Callable[..., Any] | None:
        return self._factories.get(name)


@dataclass
class Invocation:
    """Everything known about one handler invocation; the resolution context."""

    update: Any = None
    context: Any = None
    handler_name: str = "?"
    data: Any = None  # decoded callback data
    state: Any = None  # conversation state
    event: Any = None  # middleware Event
    delivery: Any = None
    error: Any = None  # set while dispatching error handlers
    extras: dict[str, Any] = field(default_factory=dict)
    principal_name: str | None = None
    auth: Auth | None = None
    _cache: dict[str, Any] = field(default_factory=dict)
    _cleanups: list[Callable[[], Any]] = field(default_factory=list)

    @property
    def bot(self) -> Any:
        if self.context is not None:
            return self.context.bot
        return self.delivery.bot if self.delivery is not None else None

    def reserved(self, name: str) -> Any:
        if name == "bot":
            return self.bot
        return getattr(self, name)

    async def aclose(self) -> None:
        """Run generator-provider cleanups (in reverse registration order)."""
        while self._cleanups:
            cleanup = self._cleanups.pop()
            await cleanup()


async def _call_factory(
    factory: Callable[..., Any],
    inv: Invocation,
    providers: Providers,
    stack: tuple[str, ...],
) -> Any:
    kwargs = await resolve_kwargs(factory, inv, providers, _stack=stack)
    if inspect.isasyncgenfunction(factory):
        agen = factory(**kwargs)
        value = await agen.__anext__()

        async def cleanup() -> None:
            try:
                await agen.__anext__()
            except StopAsyncIteration:
                pass
            else:
                raise InjectionError(f"provider {factory.__name__} yielded more than once")

        inv._cleanups.append(cleanup)
        return value

    result = factory(**kwargs)
    if inspect.isawaitable(result):
        result = await result

    return result


async def resolve_name(
    name: str,
    inv: Invocation,
    providers: Providers,
    *,
    default: Any = _MISSING,
    _stack: tuple[str, ...] = (),
) -> Any:
    if name in _stack:
        raise InjectionError(f"circular dependency: {' -> '.join((*_stack, name))}")

    if name in inv.extras:
        return inv.extras[name]

    if name in RESERVED_NAMES:
        return inv.reserved(name)

    if inv.principal_name is not None and name == inv.principal_name:
        assert inv.auth is not None
        return await inv.auth.resolve(inv, providers)

    if name in inv._cache:
        return inv._cache[name]

    value = providers.value(name)
    if value is not _MISSING:
        return value

    factory = providers.factory(name)
    if factory is not None:
        resolved = await _call_factory(factory, inv, providers, (*_stack, name))
        inv._cache[name] = resolved
        return resolved

    if default is not _MISSING:
        return default

    raise InjectionError(
        f"cannot resolve parameter {name!r} for {inv.handler_name!r}: "
        f"not a reserved name, no provider registered, and no default given"
    )


async def resolve_kwargs(
    fn: Callable[..., Any],
    inv: Invocation,
    providers: Providers,
    *,
    _stack: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build the kwargs for ``fn``, resolving each declared parameter."""
    kwargs: dict[str, Any] = {}
    for param in inspect.signature(fn).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if isinstance(param.default, Depends):
            kwargs[param.name] = await _call_factory(
                param.default.factory, inv, providers, (*_stack, param.name)
            )
            continue
        default = _MISSING if param.default is param.empty else param.default
        kwargs[param.name] = await resolve_name(
            param.name, inv, providers, default=default, _stack=_stack
        )

    return kwargs


def unresolvable_params(
    fn: Callable[..., Any],
    providers: Providers,
    *,
    extra_names: Set[str] = frozenset(),
) -> list[str]:
    """Build-time check: which params of ``fn`` have no possible source?"""
    bad: list[str] = []
    for param in inspect.signature(fn).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        if isinstance(param.default, Depends) or param.default is not param.empty:
            continue
        name = param.name
        if name in RESERVED_NAMES or name in extra_names or name in providers:
            continue
        bad.append(name)

    return bad


# ---------------------------------------------------------------- annotation lint
#
# Injection is by name, so an annotation on an injected parameter is
# documentation, not a contract -- duck-typed stand-ins are legitimate and test
# suites rely on them. But when the annotation *provably* disagrees with what
# the provider hands over, the eventual failure is an AttributeError deep
# inside a handler that names neither the parameter nor the provider. The lint
# reports only what a subclass test can settle; anything it cannot prove
# (protocols, generics, unions, unannotated factories, TYPE_CHECKING-only
# names) is left alone, because a false positive here rejects a working app.


def _checkable_class(annotation: Any) -> type | None:
    """The annotation as a concrete class a subclass test can settle, or None."""
    if annotation is None or annotation is Any:
        return None
    if not isinstance(annotation, type):
        return None  # generic aliases, unions, strings that never resolved, ...
    if getattr(annotation, "_is_protocol", False):
        return None  # a protocol is satisfied structurally, not by subclassing

    return annotation


def _resolve_lax(annotation: Any, owner: Callable[..., Any]) -> Any:
    """Evaluate a PEP 563 string annotation; ``None`` when it cannot resolve.

    An unresolvable name is almost always a TYPE_CHECKING-only import, which
    is exactly the kind of annotation the lint must leave alone.
    """
    if not isinstance(annotation, str):
        return annotation
    try:
        return eval(annotation, getattr(owner, "__globals__", {}))  # noqa: S307
    except Exception:  # noqa: BLE001
        return None


def _factory_return_type(factory: Callable[..., Any]) -> type | None:
    """What a factory provably provides: its concrete return class, or None.

    For an async generator the *yielded* type is what reaches the handler, so
    the first argument of the ``AsyncGenerator``/``AsyncIterator`` annotation
    is unwrapped; a bare or foreign return annotation proves nothing.
    """
    try:
        annotation = inspect.signature(factory).return_annotation
    except (TypeError, ValueError):
        return None
    if annotation is inspect.Parameter.empty:
        return None
    annotation = _resolve_lax(annotation, factory)

    if inspect.isasyncgenfunction(factory):
        if get_origin(annotation) not in (AsyncGenerator, AsyncIterator, AsyncIterable):
            return None
        args = get_args(annotation)
        annotation = args[0] if args else None

    return _checkable_class(annotation)


def _numeric_ok(provided: type, annotation: type) -> bool:
    """``int`` under a ``float`` annotation: the one implicit conversion Python makes."""
    return annotation is float and issubclass(provided, int)


def annotation_conflicts(
    fn: Callable[..., Any],
    providers: Providers,
    *,
    skip: Set[str] = frozenset(),
) -> list[str]:
    """Provable annotation/provider disagreements in ``fn``'s signature.

    Returns human-readable findings; empty means "nothing provable", not
    "everything checked out".
    """
    findings: list[str] = []
    fn_name = getattr(fn, "__name__", repr(fn))
    for param in inspect.signature(fn).parameters.values():
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = _checkable_class(_resolve_lax(param.annotation, fn))
        if annotation is None or param.annotation is inspect.Parameter.empty:
            continue

        # Every source reduces to the same question -- what class does it
        # provably hand over? -- so they answer it and share the verdict.
        if isinstance(param.default, Depends):
            provided = _factory_return_type(param.default.factory)
            source = f"Depends({getattr(param.default.factory, '__name__', '?')})"
        elif param.name in RESERVED_NAMES or param.name in skip:
            continue
        elif (value := providers.value(param.name)) is not _MISSING:
            provided, source = type(value), "the registered value"
        elif (factory := providers.factory(param.name)) is not None:
            provided = _factory_return_type(factory)
            source = f"provider {param.name!r}"
        else:
            continue

        if provided is None:
            continue  # nothing provable about the factory's return
        if issubclass(provided, annotation) or _numeric_ok(provided, annotation):
            continue
        findings.append(
            f"{fn_name}: parameter {param.name!r} is annotated "
            f"{annotation.__name__!r} but {source} provides {provided.__name__!r}"
        )

    return findings
