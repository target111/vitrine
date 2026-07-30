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
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Set
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


# -- provider/annotation agreement ---------------------------------------------
#
# Injection is by name, so an annotation on an injected parameter is
# documentation rather than a contract -- but when it plainly disagrees with
# what the provider hands over, the handler fails somewhere downstream with an
# AttributeError that names neither the parameter nor the provider. What
# follows reports that disagreement at build time, and only where a subclass
# test can settle it: everything else falls through unjudged, because a false
# positive here would reject a working app.

#: implicit conversions Python makes and annotations are expected to allow
_NUMERIC_TOWER: dict[type, tuple[type, ...]] = {float: (int,), complex: (int, float)}


def _nominal_class(annotation: Any) -> type | None:
    """``annotation`` as a class ``issubclass`` can judge, else ``None``.

    ``Any``, unions, generics (``list[Order]``, ``int | None``), unresolved
    forward references and protocols all mean "not decidable by subclassing" --
    a protocol most of all, since satisfying one is exactly what a stand-in
    that fails ``issubclass`` does.
    """
    if annotation is inspect.Parameter.empty or annotation is Any:
        return None
    if isinstance(annotation, str):  # a PEP 563 string nothing could resolve
        return None
    if get_origin(annotation) is not None:
        return None
    if not isinstance(annotation, type):
        return None
    if getattr(annotation, "_is_protocol", False):
        return None

    return annotation


def _resolved_signature(fn: Callable[..., Any]) -> inspect.Signature | None:
    """``fn``'s signature with real types, or ``None`` if they cannot be had.

    A ``TYPE_CHECKING``-only annotation anywhere makes the whole signature
    unevaluable; giving up on the function is right, since no check at all is
    always safe and guessing is not.
    """
    try:
        return inspect.signature(fn, eval_str=True)
    except Exception:  # noqa: BLE001 - any failure means "cannot judge"
        return None


def _yielded_class(annotation: Any) -> type | None:
    """The ``X`` an ``AsyncIterator[X]``/``AsyncGenerator[X, ...]`` provider yields."""
    if get_origin(annotation) not in (AsyncIterator, AsyncGenerator):
        return None
    args = get_args(annotation)

    return _nominal_class(args[0]) if args else None


def _factory_supplies(factory: Callable[..., Any]) -> type | None:
    """What ``factory`` promises to return, when it promises anything."""
    signature = _resolved_signature(factory)
    if signature is None:
        return None

    annotation = signature.return_annotation
    if inspect.isasyncgenfunction(factory):
        return _yielded_class(annotation)

    return _nominal_class(annotation)


def _supplied_class(name: str, providers: Providers) -> type | None:
    value = providers.value(name)
    if value is not _MISSING:
        return type(value)

    factory = providers.factory(name)

    return _factory_supplies(factory) if factory is not None else None


def type_mismatches(
    fn: Callable[..., Any],
    providers: Providers,
    *,
    skip: Set[str] = frozenset(),
) -> list[tuple[str, type, type]]:
    """``(parameter, annotated, supplied)`` for each provably wrong annotation.

    Empty whenever anything is uncertain -- an unevaluable signature, a
    protocol, a generic, a factory that annotates no return type.
    """
    signature = _resolved_signature(fn)
    if signature is None:
        return []

    bad: list[tuple[str, type, type]] = []
    for param in signature.parameters.values():
        if param.name in skip or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue

        wanted = _nominal_class(param.annotation)
        if wanted is None:
            continue

        if isinstance(param.default, Depends):
            supplied = _factory_supplies(param.default.factory)
        else:
            supplied = _supplied_class(param.name, providers)
        if supplied is None:
            continue

        try:
            if issubclass(supplied, wanted) or supplied in _NUMERIC_TOWER.get(wanted, ()):
                continue
        except TypeError:  # an exotic metaclass; not ours to judge
            continue

        bad.append((param.name, wanted, supplied))

    return bad
