"""Dependency injection into handlers.

Handlers stay plain async functions; they declare what they need by parameter
name and the framework supplies it. Three sources, in priority order:

1. explicit ``Depends(factory)`` defaults,
2. per-invocation values (``update``, ``context``, ``bot``, ``data``, ``state``,
   ``event``, ``delivery``, parsed command args, middleware extras, principal),
3. registered providers (``bot.provide("db")(make_db)``).

Provider factories may themselves declare parameters (resolved recursively),
may be sync or async, and may be async *generators* — the part after ``yield``
runs as cleanup, FastAPI-style::

    @bot.provide("session")
    async def session(db):          # "db" comes from another provider
        async with db.begin() as s:
            yield s

Resolved providers live in a :class:`DependencyScope`. While the bot handles
an update there is one scope for the whole update -- the ban gate, every
handler group, conversation hooks and error handlers share it -- and its
cleanups run once the update is fully processed. So the principal and the
session it was loaded with stay alive together. Workers, lifecycle hooks and
anything outside an update get a scope of their own per call.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import (
    AsyncGenerator,
    AsyncIterable,
    AsyncIterator,
    Awaitable,
    Callable,
    Set,
)
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, get_args, get_origin

from .exceptions import InjectionError

if TYPE_CHECKING:
    from .auth import Auth

logger = logging.getLogger("vitrine.injection")

#: names the framework supplies without any provider registration
RESERVED_NAMES = frozenset(
    {"update", "context", "bot", "data", "state", "event", "delivery", "error"}
)

_MISSING: Any = object()


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


class _Teardown:
    """A generator provider's pending cleanup: finish it, or abort it."""

    __slots__ = ("agen", "name")

    def __init__(self, agen: AsyncGenerator[Any, None], name: str) -> None:
        self.agen = agen
        self.name = name

    async def finish(self) -> None:
        """Run the part after ``yield`` (a commit, say)."""
        try:
            await self.agen.__anext__()
        except StopAsyncIteration:
            return
        raise InjectionError(f"provider {self.name} yielded more than once")

    async def abort(self) -> None:
        """Close it at the ``yield`` instead: ``async with`` blocks unwind (roll back)."""
        await self.agen.aclose()


@dataclass
class _Journal:
    """What one resolution added to a scope, so a failure can take it back."""

    names: list[str] = field(default_factory=list)
    teardowns: list[_Teardown] = field(default_factory=list)


class DependencyScope:
    """Resolved providers, the resolved principal, and the cleanups they owe.

    An update's scope is held by its processing and by every invocation that
    joined it; it closes when the last of them lets go.
    """

    def __init__(
        self,
        update: Any = None,
        on_cleanup_error: Callable[[Exception], Awaitable[Any]] | None = None,
    ) -> None:
        self.update = update
        self.cache: dict[str, Any] = {}
        self.cleanups: list[_Teardown] = []
        self.principal: Any = _MISSING
        self.closed = False
        self._holds = 0
        self._on_cleanup_error = on_cleanup_error

    def hold(self) -> None:
        self._holds += 1

    async def release(self) -> None:
        """Drop a hold; the last one closes the scope, reporting a failed cleanup."""
        self._holds -= 1
        if self._holds > 0:
            return
        try:
            await self.aclose()
        except Exception as exc:
            if self._on_cleanup_error is None:
                logger.exception(
                    "provider cleanup failed after update %s", _update_id(self.update)
                )
            else:
                await self._on_cleanup_error(exc)

    async def aclose(self) -> None:
        """Run generator-provider cleanups, newest first; each one runs regardless."""
        self.closed = True
        failures: list[BaseException] = []
        while self.cleanups:
            teardown = self.cleanups.pop()
            try:
                await teardown.finish()
            except Exception as exc:  # noqa: BLE001 - the others must still run
                failures.append(exc)
        # anything still holding the scope (a timeout timer's context) mustn't
        # keep what the update resolved alive
        self.cache.clear()
        self.principal = _MISSING
        if failures:
            raise failures[0]

    async def undo(self, journal: _Journal) -> None:
        """Forget what ``journal`` resolved and abort its generator providers."""
        for name in journal.names:
            self.cache.pop(name, None)
        for teardown in reversed(journal.teardowns):
            if teardown not in self.cleanups:
                continue  # the scope closed meanwhile and already finished it
            self.cleanups.remove(teardown)
            try:
                await teardown.abort()
            except Exception:
                logger.exception("provider %s failed to abort", teardown.name)


#: the scope of the update being processed, set by VitrineApplication
_update_scope: ContextVar[DependencyScope | None] = ContextVar(
    "vitrine_update_scope", default=None
)


def update_scope(update: Any) -> DependencyScope | None:
    """The open scope for ``update`` if the application is processing it."""
    scope = _update_scope.get()
    if scope is None or scope.closed or update is None or scope.update is not update:
        return None
    return scope


def join_update_scope(update: Any) -> tuple[DependencyScope, bool]:
    """``(scope, owned)``: the update's open scope, held until the invocation
    closes -- it may outlive the update's processing (a handler PTB runs as a
    task) -- or, outside an update, a fresh scope of the caller's own."""
    shared = update_scope(update)
    if shared is None:
        return DependencyScope(update), True
    shared.hold()
    return shared, False


async def run_in_update_scope(
    update: Any,
    process: Callable[[], Awaitable[Any]],
    on_cleanup_error: Callable[[Exception], Awaitable[Any]] | None = None,
) -> None:
    """Process one update inside a fresh scope, closing it afterwards.

    A failing cleanup (say, a session's commit) goes to ``on_cleanup_error``
    -- the bot's error handlers -- so the caller still gets error UX.
    """
    scope = DependencyScope(update, on_cleanup_error)
    scope.hold()
    token = _update_scope.set(scope)
    try:
        await process()
    finally:
        _update_scope.reset(token)
        await scope.release()


@asynccontextmanager
async def all_or_nothing(inv: Invocation) -> AsyncIterator[None]:
    """Resolve in ``inv``'s scope, taking it all back if the block fails.

    A failed resolution mustn't leave half its providers cached for the rest of
    the update -- a session whose transaction it aborted, say.
    """
    outer, journal = inv.journal, _Journal()
    inv.journal = journal
    try:
        yield
    except BaseException:
        await inv.scope.undo(journal)
        raise
    finally:
        inv.journal = outer
    if outer is not None:
        outer.names += journal.names
        outer.teardowns += journal.teardowns


def _update_id(update: Any) -> Any:
    return getattr(update, "update_id", None)


@dataclass
class Invocation:
    """Everything known about one handler invocation; the resolution context.

    Per-handler values live here; resolved providers and the principal live
    in :attr:`scope`, which several invocations of one update share. An
    invocation closes a scope it created (``owns_scope``) and releases one it
    joined.
    """

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
    scope: DependencyScope = field(default_factory=DependencyScope)
    owns_scope: bool = True
    journal: _Journal | None = None  # set inside all_or_nothing()

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
        if self.owns_scope:
            await self.scope.aclose()
        else:
            await self.scope.release()


async def _call_factory(
    factory: Callable[..., Any],
    inv: Invocation,
    providers: Providers,
    stack: tuple[str, ...],
) -> Any:
    kwargs = await resolve_kwargs(factory, inv, providers, _stack=stack)
    if inspect.isasyncgenfunction(factory):
        if inv.scope.closed:
            # its cleanup would never run
            raise InjectionError(
                f"cannot resolve {factory.__name__} for {inv.handler_name!r}: the "
                f"update's dependency scope is closed (a task that outlived its update?)"
            )
        agen = factory(**kwargs)
        value = await agen.__anext__()
        teardown = _Teardown(agen, factory.__name__)
        inv.scope.cleanups.append(teardown)
        if inv.journal is not None:
            inv.journal.teardowns.append(teardown)
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

    if name in inv.scope.cache:
        return inv.scope.cache[name]

    value = providers.value(name)
    if value is not _MISSING:
        return value

    factory = providers.factory(name)
    if factory is not None:
        resolved = await _call_factory(factory, inv, providers, (*_stack, name))
        inv.scope.cache[name] = resolved
        if inv.journal is not None:
            inv.journal.names.append(name)
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
# Injection is by name, so annotations on injected parameters are documentation
# and duck-typed stand-ins are fine. The lint reports only disagreements a
# subclass test can prove; anything else (protocols, generics, unions,
# unannotated factories, TYPE_CHECKING-only names) is left alone.


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
