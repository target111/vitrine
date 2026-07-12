"""Typed error -> UX dispatch.

Register UX for an exception type with one decorator; dispatch picks the most
specific registered type by MRO::

    @bot.on_error(InsufficientFunds)
    async def insufficient(error, user):
        return Screen(
            text=Md().line("Not enough balance: need ", code(error.needed)),
            keyboard=[[Button("« Back", callback=MenuCB(section="wallet"))]],
        )

An error handler may return a :class:`Screen` — it is rendered *into the
current screen* (edit-in-place for callback updates, reply otherwise) — or
``None`` after doing its own thing. Friendly defaults are pre-registered for
all framework errors, and a catch-all keeps unexpected exceptions from ever
reaching the user raw.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable, Type

from telegram.error import TelegramError

from .exceptions import UsageError, UserFacingError
from .injection import Invocation, Providers, resolve_kwargs
from .markdown import Md, code
from .screens import Screen

logger = logging.getLogger("vitrine.errors")

ErrorHandler = Callable[..., Awaitable[Any]]


async def _answer_or_reply(
    inv: Invocation, text: str, *, show_alert: bool = False
) -> None:
    """Lightweight fallback UX when a handler doesn't render a screen."""
    update = inv.update
    query = getattr(update, "callback_query", None) if update else None
    if query is not None:
        try:
            await query.answer(text[:200], show_alert=show_alert)
            return
        except TelegramError as exc:
            logger.debug("could not answer callback query: %s", exc)

    message = getattr(update, "effective_message", None) if update else None
    if message is not None:
        try:
            await message.reply_text(text)
        except TelegramError as exc:
            logger.debug("could not send error reply: %s", exc)


class ErrorRegistry:
    def __init__(self) -> None:
        self._handlers: dict[Type[BaseException], ErrorHandler] = {}
        self._register_defaults()

    def on(
        self, exc_type: Type[BaseException]
    ) -> Callable[[ErrorHandler], ErrorHandler]:
        """Decorator: register (or override) UX for an exception type."""

        def register(fn: ErrorHandler) -> ErrorHandler:
            self._handlers[exc_type] = fn
            return fn

        return register

    def find(self, error: BaseException) -> ErrorHandler | None:
        """Most specific registered handler wins, walking the MRO."""
        for cls in type(error).__mro__:
            handler = self._handlers.get(cls)  # type: ignore[arg-type]
            if handler is not None:
                return handler

        return None

    async def dispatch(
        self, error: BaseException, inv: Invocation, providers: Providers
    ) -> None:
        """Run the matching handler; render a returned Screen into the current one."""
        handler = self.find(error)
        if handler is None:
            await self._last_resort(error, inv)
            return

        inv.error = error
        try:
            kwargs = await resolve_kwargs(handler, inv, providers)
            result = await handler(**kwargs)
        except Exception:
            logger.exception(
                "error handler %s itself failed while handling %r",
                getattr(handler, "__name__", handler),
                error,
            )
            await self._last_resort(error, inv)
            return

        if isinstance(result, Screen):
            if inv.delivery is not None and inv.update is not None:
                await inv.delivery.render(inv.update, result)
            elif inv.update is not None and inv.context is not None:
                await result.render(inv.update, inv.context)

    async def _last_resort(self, error: BaseException, inv: Invocation) -> None:
        logger.exception("unhandled error in %s", inv.handler_name, exc_info=error)
        await _answer_or_reply(inv, "Something went wrong. Please try again.")

    def _register_defaults(self) -> None:
        @self.on(Exception)
        async def unexpected(error: BaseException, update: Any, context: Any) -> None:
            logger.exception("unhandled error", exc_info=error)
            inv = Invocation(update=update, context=context)
            await _answer_or_reply(inv, "Something went wrong. Please try again.")

        @self.on(UserFacingError)
        async def user_facing(
            error: UserFacingError, update: Any, context: Any
        ) -> None:
            inv = Invocation(update=update, context=context)
            await _answer_or_reply(inv, error.message, show_alert=error.show_alert)

        @self.on(UsageError)
        async def usage(error: UsageError, update: Any, context: Any) -> Screen | None:
            message = getattr(update, "effective_message", None) if update else None
            if message is None:
                return None

            doc = Md()
            if error.hint:
                doc.line(error.hint)
            if error.usage:
                doc.line("Usage: ", code(error.usage))

            await message.reply_text(doc.render(2), parse_mode="MarkdownV2")
            return None

        @self.on(TelegramError)
        async def telegram_error(
            error: TelegramError, update: Any, context: Any
        ) -> None:
            logger.warning("telegram API error: %s", error)
            inv = Invocation(update=update, context=context)
            await _answer_or_reply(inv, "Telegram hiccuped. Please try again.")
