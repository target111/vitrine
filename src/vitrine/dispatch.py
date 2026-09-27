"""The handler pipeline: one code path every framework handler goes through.

For each update:  decode callback data -> middleware chain -> guards ->
throttle -> parse command args -> inject -> call handler -> handle the result
(render a returned Screen, or a conversation's transition) -> error UX ->
one structured log line.

Conversations run their steps and exit hooks through :meth:`Dispatch.run`;
workers and lifecycle hooks use :meth:`Dispatch.invocation` for injection
without an update.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from telegram.error import TelegramError

from .args import ArgSpec, build_arg_specs, parse_args
from .auth import Auth, has_guards
from .callbacks import CallbackDataError
from .errors import ErrorRegistry
from .exceptions import ConfigurationError
from .injection import (
    Invocation,
    Providers,
    annotation_conflicts,
    join_update_scope,
    resolve_kwargs,
    unresolvable_params,
)
from .logging import log_event
from .middleware import Event, Middleware, compose
from .ratelimit import RateLimiter, _Drop, throttle_spec
from .routing import Registration
from .screens import Delivery, Screen

logger = logging.getLogger("vitrine.update")
build_logger = logging.getLogger("vitrine.build")

EXPIRED_BUTTON_TEXT = "This button has expired."

#: turns a handler's return value into the pipeline's result, inside the
#: error-handled chain: the default renders a returned Screen
ResultHandler = Callable[[Any], Awaitable[Any]]


def command_arg_text(update: Any) -> str | None:
    """The raw text after ``/command`` (bot-mention stripped by the split)."""
    message = getattr(update, "effective_message", None)
    text = getattr(message, "text", None) if message else None
    if not text:
        return None

    _, _, rest = text.partition(" ")
    return rest


class Dispatch:
    def __init__(
        self,
        providers: Providers,
        errors: ErrorRegistry,
        limiter: RateLimiter,
        delivery: Delivery,
        auth: Auth | None = None,
        middlewares: list[Middleware] | None = None,
        *,
        strict_types: bool = False,
    ) -> None:
        self.providers = providers
        self.errors = errors
        self.limiter = limiter
        self.delivery = delivery
        self.auth = auth
        self.middlewares = middlewares or []  # bot-scoped, outermost
        self.strict_types = strict_types
        self._arg_specs: dict[int, list[ArgSpec]] = {}

    # -- wiring -----------------------------------------------------------------

    def invocation(
        self, handler_name: str, update: Any = None, context: Any = None
    ) -> Invocation:
        """A resolution context; joins the update's scope when one is open."""
        scope, owned = join_update_scope(update)
        return Invocation(
            update=update,
            context=context,
            handler_name=handler_name,
            delivery=self.delivery,
            principal_name=self.auth.name if self.auth else None,
            auth=self.auth,
            scope=scope,
            owns_scope=owned,
        )

    def arg_specs(self, reg: Registration) -> list[ArgSpec]:
        """The command arguments a registration parses (computed once)."""
        cached = self._arg_specs.get(id(reg.fn))
        if cached is None:
            cached = build_arg_specs(reg.fn)
            self._arg_specs[id(reg.fn)] = cached

        return cached

    def validate(self, reg: Registration) -> None:
        """Build-time check: every handler param must have a source.

        Also reports annotations that provably disagree with their provider:
        a warning on the ``vitrine.build`` logger, or a build failure under
        ``strict_types``.
        """
        specs = self.arg_specs(reg)
        if specs and reg.kind != "command":
            raise ConfigurationError(
                f"handler {reg.name!r} declares command argument(s) "
                f"{[spec.name for spec in specs]}, but it is a {reg.kind} handler: "
                f"only commands have a command line to parse"
            )

        extra = {self.auth.name} if self.auth else set()
        extra = extra | {spec.name for spec in specs}

        bad = unresolvable_params(reg.fn, self.providers, extra_names=extra)
        if bad:
            hint = (
                "; if it is a command argument, annotate it as Arg[...]"
                if reg.kind == "command"
                else ""
            )
            raise ConfigurationError(
                f"handler {reg.name!r} declares parameter(s) {bad} that nothing can "
                f"supply: not reserved, not a provider, not the principal{hint}"
            )

        conflicts = annotation_conflicts(reg.fn, self.providers, skip=extra)
        if conflicts and self.strict_types:
            raise ConfigurationError("; ".join(conflicts))
        for finding in conflicts:
            build_logger.warning("%s", finding)

    def ptb_callback(
        self, reg: Registration
    ) -> Callable[[Any, Any], Coroutine[Any, Any, Any]]:
        async def handle(update: Any, context: Any) -> Any:
            return await self.run(reg, update, context)

        return handle

    # -- the pipeline -------------------------------------------------------------

    async def run(
        self,
        reg: Registration,
        update: Any,
        context: Any,
        *,
        state: Any = None,
        extras: dict[str, Any] | None = None,
        on_result: ResultHandler | None = None,
        answer_query: bool = True,
    ) -> Any:
        """Run one handler; returns ``on_result``'s value, or None on failure."""
        started = time.monotonic()
        inv = self.invocation(reg.name, update, context)
        inv.state = state
        inv.extras.update(extras or {})

        # Decode typed callback data first: malformed/stale data must fail safely.
        if reg.cb_model is not None:
            query = getattr(update, "callback_query", None)
            raw = getattr(query, "data", None)
            try:
                inv.data = reg.cb_model.unpack(raw or "")
            except CallbackDataError as exc:
                logger.warning("stale/invalid callback data in %s: %s", reg.name, exc)
                await self._answer_query(update, EXPIRED_BUTTON_TEXT)
                await inv.aclose()
                return None

        event = Event(
            update=update,
            context=context,
            handler_name=reg.name,
            data=inv.data,
            state=state,
            extras=inv.extras,  # shared: middleware extras become injectable
        )
        inv.event = event
        handle_result = on_result or self._render_screen(update)

        async def core(evt: Event) -> Any:
            if self.auth is not None and has_guards(reg.fn):
                principal = await self.auth.resolve(inv, self.providers)
                self.auth.check(reg.fn, principal)

            spec = throttle_spec(reg.fn)
            if spec is not None:
                await self.limiter.enforce(spec, evt)

            specs = self.arg_specs(reg) if reg.kind == "command" else []
            if specs:
                assert reg.command is not None
                inv.extras.update(parse_args(reg.command, specs, command_arg_text(update)))

            kwargs = await resolve_kwargs(reg.fn, inv, self.providers)
            result = reg.fn(**kwargs)
            if hasattr(result, "__await__"):
                result = await result

            return await handle_result(result)

        chain = compose([*self.middlewares, *reg.middlewares], core)
        status = "ok"
        result: Any = None
        try:
            result = await chain(event)
        except _Drop:
            status = "throttled"
        except Exception as exc:  # noqa: BLE001 - the error layer owns UX
            status = f"error:{type(exc).__name__}"
            await self.errors.dispatch(exc, inv, self.providers)
        finally:
            await inv.aclose()
            if answer_query:
                await self._answer_query(update)
            user = getattr(update, "effective_user", None)
            chat = getattr(update, "effective_chat", None)
            log_event(
                logger,
                "update.handled",
                handler=reg.name,
                user=getattr(user, "id", None),
                chat=getattr(chat, "id", None),
                ms=round((time.monotonic() - started) * 1000),
                status=status,
            )

        return result

    def _render_screen(self, update: Any) -> ResultHandler:
        async def render(result: Any) -> Any:
            if isinstance(result, Screen):
                await self.delivery.render(update, result)
            return result

        return render

    @staticmethod
    async def _answer_query(update: Any, text: str | None = None) -> None:
        """Stop the button spinner; harmless if already answered or expired."""
        query = getattr(update, "callback_query", None)
        if query is None:
            return

        try:
            await query.answer(text)
        except TelegramError as exc:
            logger.debug("answer_callback_query failed: %s", exc)
