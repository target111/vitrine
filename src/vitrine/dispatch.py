"""The handler pipeline: one code path every framework handler goes through.

For each update:  decode callback data -> middleware chain -> guards ->
throttle -> parse command args -> inject -> call handler -> render returned
Screen -> error UX -> cleanups -> one structured log line.

Conversations reuse ``Dispatch.run`` with a state object; workers and lifecycle
hooks reuse ``Dispatch.invocation`` for injection without an update.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Coroutine
from typing import Any

from telegram.error import TelegramError

from .args import ArgSpec, build_arg_specs, parse_args
from .auth import Auth, has_guards
from .callbacks import CallbackDataError
from .exceptions import ConfigurationError
from .injection import (
    RESERVED_NAMES,
    Invocation,
    Providers,
    resolve_kwargs,
    unresolvable_params,
)
from .logging import log_event
from .middleware import Event, Middleware, compose
from .ratelimit import RateLimiter, _Drop, throttle_spec
from .routing import Registration
from .screens import Delivery, Screen

logger = logging.getLogger("vitrine.update")

EXPIRED_BUTTON_TEXT = "This button has expired."


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
        errors: Any,
        limiter: RateLimiter,
        auth: Auth | None = None,
        middlewares: list[Middleware] | None = None,
    ) -> None:
        self.providers = providers
        self.errors = errors
        self.limiter = limiter
        self.auth = auth
        self.middlewares = middlewares or []  # bot-scoped, outermost
        self.delivery: Delivery | None = None  # attached at build time
        self._arg_specs: dict[int, list[ArgSpec]] = {}

    # -- wiring -----------------------------------------------------------------

    def invocation(
        self, handler_name: str, update: Any = None, context: Any = None
    ) -> Invocation:
        return Invocation(
            update=update,
            context=context,
            handler_name=handler_name,
            delivery=self.delivery,
            principal_name=self.auth.name if self.auth else None,
            auth=self.auth,
        )

    def arg_specs(self, reg: Registration) -> list[ArgSpec]:
        """Compute (once) which params of a command handler are typed arguments."""
        cached = self._arg_specs.get(id(reg.fn))
        if cached is None:
            skip = RESERVED_NAMES | self.providers.names()
            if self.auth is not None:
                skip = skip | {self.auth.name}
            cached = build_arg_specs(reg.fn, skip)
            self._arg_specs[id(reg.fn)] = cached

        return cached

    def validate(self, reg: Registration) -> None:
        """Build-time check: every handler param must have a source."""
        extra = {self.auth.name} if self.auth else set()
        if reg.kind == "command":
            extra = extra | {spec.name for spec in self.arg_specs(reg)}

        bad = unresolvable_params(reg.fn, self.providers, extra_names=extra)
        if bad:
            raise ConfigurationError(
                f"handler {reg.name!r} declares parameter(s) {bad} that nothing can "
                f"supply: not reserved, not a provider, not a command argument"
            )

    def ptb_callback(
        self, reg: Registration
    ) -> Callable[[Any, Any], Coroutine[Any, Any, Any]]:
        async def handle(update: Any, context: Any) -> Any:
            return await self.run(reg, update, context)

        return handle

    # -- the pipeline -------------------------------------------------------------

    async def run(
        self, reg: Registration, update: Any, context: Any, *, state: Any = None
    ) -> Any:
        started = time.monotonic()
        inv = self.invocation(reg.name, update, context)
        inv.state = state

        # Decode typed callback data first: malformed/stale data must fail safely.
        if reg.cb_model is not None:
            query = getattr(update, "callback_query", None)
            raw = getattr(query, "data", None)
            try:
                inv.data = reg.cb_model.unpack(raw or "")
            except CallbackDataError as exc:
                logger.warning("stale/invalid callback data in %s: %s", reg.name, exc)
                await self._answer_query(update, EXPIRED_BUTTON_TEXT)
                return None

            if reg.cb_when is not None and not reg.cb_when(inv.data):
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

        async def core(evt: Event) -> Any:
            if self.auth is not None and has_guards(reg.fn):
                principal = await self.auth.resolve(inv, self.providers)
                self.auth.check(reg.fn, principal)

            spec = throttle_spec(reg.fn)
            if spec is not None:
                await self.limiter.enforce(spec, evt)

            if reg.kind == "command":
                specs = self.arg_specs(reg)
                if specs:
                    assert reg.command is not None
                    inv.extras.update(
                        parse_args(reg.command, specs, command_arg_text(update))
                    )

            kwargs = await resolve_kwargs(reg.fn, inv, self.providers)
            result = reg.fn(**kwargs)
            if hasattr(result, "__await__"):
                result = await result

            if isinstance(result, Screen):
                await self._render(update, context, result)

            return result

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

    async def _render(self, update: Any, context: Any, screen: Screen) -> None:
        delivery = self.delivery or Delivery(context.bot)
        await delivery.render(update, screen)

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
