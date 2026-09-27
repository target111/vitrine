"""Test a bot through the real PTB dispatcher, with Telegram faked at the HTTP layer.

:class:`TestClient` builds your :class:`~vitrine.Bot` into a real PTB
``Application`` whose HTTP transport is :class:`FakeTelegram`. Updates go
through ``Application.process_update`` -- handler groups, filters, the ban
gate, conversations and error handlers all run exactly as in production --
and every Bot API call the bot makes is recorded instead of sent::

    from vitrine.testing import TestClient

    async def test_start():
        async with TestClient(build_bot()) as client:
            await client.send("/start")
            assert "Welcome" in client.last_text()

            await client.press(MenuCB(section="shop"))  # on the bot's last message
            assert client.calls("editMessageText")
"""

from __future__ import annotations

import datetime
import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from telegram import Update
from telegram.request import BaseRequest, RequestData

from .callbacks import CallbackData
from .screens import _SEND_METHODS

if TYPE_CHECKING:
    from telegram.ext import Application

    from .app import Bot

__all__ = ["ApiCall", "FakeTelegram", "TestClient", "sent_texts"]

BOT_ID = 999_999
BOT_USERNAME = "vitrine_test_bot"


@dataclass
class ApiCall:
    """One recorded Bot API request."""

    method: str  # the API name, e.g. "sendMessage"
    params: dict[str, Any]

    @property
    def text(self) -> str | None:
        """The message text or caption this call carried, if any."""
        value = self.params.get("text", self.params.get("caption"))
        if value is None and isinstance(self.params.get("media"), dict):
            value = self.params["media"].get("caption")
        return value


#: Bot API method -> media kind, e.g. "sendVideoNote" -> "video_note"
_MEDIA_SENDS = {
    "".join(part.title() if i else part for i, part in enumerate(method.split("_"))): kind
    for kind, (method, _) in _SEND_METHODS.items()
}


class FakeTelegram(BaseRequest):
    """A Bot API that records every request and answers with plausible results.

    ``fail(method, description)`` queues an error response, which PTB turns
    into the same exception a real Telegram error would raise (``BadRequest``,
    ``RetryAfter`` with ``retry_after=``, ...).
    """

    def __init__(self) -> None:
        self.requests: list[ApiCall] = []
        self._failures: dict[str, list[tuple[int, str, int | None]]] = {}
        self._message_ids = itertools.count(10_000)
        self._file_ids = itertools.count(1)
        #: (chat_id, message_id) -> the message as the bot last left it
        self.messages: dict[tuple[int, int], dict[str, Any]] = {}

    @property
    def read_timeout(self) -> float | None:
        return None

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def fail(
        self,
        method: str,
        description: str,
        *,
        code: int = 400,
        retry_after: int | None = None,
        times: int = 1,
    ) -> None:
        """Answer the next ``times`` calls to ``method`` with an error."""
        if retry_after is not None:
            code = 429
        self._failures.setdefault(method, []).extend(
            [(code, description, retry_after)] * times
        )

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = None,
        write_timeout: Any = None,
        connect_timeout: Any = None,
        pool_timeout: Any = None,
    ) -> tuple[int, bytes]:
        api = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        self.requests.append(ApiCall(api, params))

        queue = self._failures.get(api)
        if queue:
            code, description, retry_after = queue.pop(0)
            body: dict[str, Any] = {
                "ok": False,
                "error_code": code,
                "description": description,
            }
            if retry_after is not None:
                body["parameters"] = {"retry_after": retry_after}
            return code, json.dumps(body).encode()

        return 200, json.dumps({"ok": True, "result": self._result(api, params)}).encode()

    # -- canned results ----------------------------------------------------------

    def _result(self, api: str, params: dict[str, Any]) -> Any:
        if api == "getMe":
            return {
                "id": BOT_ID,
                "is_bot": True,
                "first_name": "Test",
                "username": BOT_USERNAME,
            }
        if api == "sendMessage":
            return self._store(self._message(params, text=params.get("text")))
        if api in _MEDIA_SENDS:
            kind = _MEDIA_SENDS[api]
            return self._store(
                self._message(params, media=kind, caption=params.get("caption"))
            )
        if api == "editMessageText":
            return self._edit(params, text=params.get("text"))
        if api == "editMessageMedia":
            media = params.get("media") or {}
            return self._edit(
                params, media=media.get("type", "photo"), caption=media.get("caption")
            )
        if api == "deleteMessage":
            self.messages.pop((int(params["chat_id"]), int(params["message_id"])), None)
        return True

    def _media(self, kind: str) -> Any:
        ids = {"file_id": f"file-{next(self._file_ids)}", "file_unique_id": "u"}
        return {
            "photo": [{**ids, "width": 1, "height": 1}],
            "video": {**ids, "width": 1, "height": 1, "duration": 1},
            "animation": {**ids, "width": 1, "height": 1, "duration": 1},
            "document": ids,
            "audio": {**ids, "duration": 1},
            "voice": {**ids, "duration": 1},
            "video_note": {**ids, "length": 1, "duration": 1},
            "sticker": {
                **ids,
                "type": "regular",
                "width": 1,
                "height": 1,
                "is_animated": False,
                "is_video": False,
            },
        }[kind]

    def _message(
        self,
        params: dict[str, Any],
        *,
        message_id: int | None = None,
        text: str | None = None,
        media: str | None = None,
        caption: str | None = None,
    ) -> dict[str, Any]:
        chat_id = int(params["chat_id"])
        message: dict[str, Any] = {
            "message_id": message_id or next(self._message_ids),
            "date": int(datetime.datetime.now(datetime.UTC).timestamp()),
            "chat": _chat(chat_id),
            "from": {"id": BOT_ID, "is_bot": True, "first_name": "Test"},
        }
        if text is not None:
            message["text"] = text
        if media is not None:
            message[media] = self._media(media)
            if caption is not None:
                message["caption"] = caption
        if isinstance(params.get("reply_markup"), dict) and (
            "inline_keyboard" in params["reply_markup"]
        ):
            message["reply_markup"] = params["reply_markup"]
        return message

    def _store(self, message: dict[str, Any]) -> dict[str, Any]:
        self.messages[(message["chat"]["id"], message["message_id"])] = message
        return message

    def _edit(self, params: dict[str, Any], **content: Any) -> Any:
        if "chat_id" not in params:  # an inline message: Telegram answers True
            return True
        message = self._message(params, message_id=int(params["message_id"]), **content)
        return self._store(message)


def _chat(chat_id: int) -> dict[str, Any]:
    if chat_id > 0:
        return {"id": chat_id, "type": "private", "first_name": "Test"}
    return {"id": chat_id, "type": "group", "title": "Test group"}


class TestClient:
    """Drive a :class:`~vitrine.Bot` update by update, as a Telegram user would.

    Each ``send``/``press``/``edit`` returns the API calls the bot made while
    handling that one update. ``startup=True`` also runs the bot's startup
    path (hooks, workers, command menu sync); it is off by default so periodic
    workers don't send messages in the middle of a test.

    The client builds ``bot`` against its own :class:`FakeTelegram`, so each
    client needs a fresh bot (from a factory); a bot built for another client
    raises :class:`~vitrine.exceptions.ConfigurationError`.
    """

    __test__ = False  # not a pytest test class, despite the name

    def __init__(
        self,
        bot: Bot[Any],
        *,
        user_id: int = 42,
        chat_id: int | None = None,
        startup: bool = False,
    ) -> None:
        self.bot = bot
        self.user_id = user_id
        self.chat_id = chat_id if chat_id is not None else user_id
        self.telegram = FakeTelegram()
        self._startup = startup
        self._update_ids = itertools.count(1)
        self._user_message_ids = itertools.count(1)
        self._query_ids = itertools.count(1)
        self._app: Application | None = None

    @property
    def app(self) -> Application:
        if self._app is None:
            raise RuntimeError(
                "TestClient is not running; use `async with TestClient(...)`"
            )
        return self._app

    async def __aenter__(self) -> TestClient:
        if not self.bot.token:
            self.bot.token = "123456:TEST"
        self._app = self.bot.build(request=self.telegram)
        await self._app.initialize()
        if self._startup:
            await self.bot.startup()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.bot.stop()
        await self.app.shutdown()
        if self._startup:
            await self.bot.shutdown()

    # -- updates ---------------------------------------------------------------

    async def feed(self, update: Update | dict[str, Any]) -> list[ApiCall]:
        """Process one update (an ``Update`` or its Bot API dict form)."""
        if isinstance(update, dict):
            update = Update.de_json(
                {"update_id": next(self._update_ids), **update}, self.app.bot
            )
        before = len(self.telegram.requests)
        await self.app.process_update(update)
        return self.telegram.requests[before:]

    async def send(
        self, text: str, *, user_id: int | None = None, chat_id: int | None = None
    ) -> list[ApiCall]:
        """The user sends a text message (a ``/command`` if it starts with one)."""
        return await self.feed({"message": self._user_message(text, user_id, chat_id)})

    async def edit(
        self,
        message_id: int,
        text: str,
        *,
        user_id: int | None = None,
        chat_id: int | None = None,
    ) -> list[ApiCall]:
        """The user edits one of their earlier messages into ``text``."""
        message = self._user_message(text, user_id, chat_id, message_id=message_id)
        message["edit_date"] = message["date"]
        return await self.feed({"edited_message": message})

    async def press(
        self,
        data: CallbackData | str,
        *,
        message_id: int | None = None,
        user_id: int | None = None,
        chat_id: int | None = None,
    ) -> list[ApiCall]:
        """The user presses an inline button on one of the bot's messages.

        The button sits on the bot's most recent message in the chat unless
        ``message_id`` names another.
        """
        chat = chat_id if chat_id is not None else self.chat_id
        message = (
            self.telegram.messages.get((chat, message_id))
            if message_id is not None
            else self.last_message(chat)
        )
        query: dict[str, Any] = {
            "id": str(next(self._query_ids)),
            "from": self._user(user_id),
            "chat_instance": "test",
            "data": data.pack() if isinstance(data, CallbackData) else data,
        }
        if message is not None:
            query["message"] = message
        return await self.feed({"callback_query": query})

    # -- inspection ------------------------------------------------------------

    @property
    def requests(self) -> list[ApiCall]:
        return self.telegram.requests

    def calls(self, method: str) -> list[ApiCall]:
        """Every recorded call to one API method, e.g. ``"sendMessage"``."""
        return [call for call in self.requests if call.method == method]

    def texts(self) -> list[str]:
        """Every text and caption the bot sent or edited in, in order."""
        return sent_texts(self.requests)

    def last_text(self) -> str | None:
        texts = self.texts()
        return texts[-1] if texts else None

    def last_message(self, chat_id: int | None = None) -> dict[str, Any] | None:
        """The bot's newest surviving message in a chat, as a Bot API dict."""
        chat = chat_id if chat_id is not None else self.chat_id
        mine = [m for (c, _), m in self.telegram.messages.items() if c == chat]
        return max(mine, key=lambda m: m["message_id"]) if mine else None

    # -- construction ----------------------------------------------------------

    def _user(self, user_id: int | None) -> dict[str, Any]:
        return {"id": user_id or self.user_id, "is_bot": False, "first_name": "User"}

    def _user_message(
        self,
        text: str,
        user_id: int | None,
        chat_id: int | None,
        *,
        message_id: int | None = None,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "message_id": message_id or next(self._user_message_ids),
            "date": int(datetime.datetime.now(datetime.UTC).timestamp()),
            "chat": _chat(chat_id if chat_id is not None else self.chat_id),
            "from": self._user(user_id),
            "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
        return message


def sent_texts(calls: Sequence[ApiCall]) -> list[str]:
    """The texts among a batch of calls, e.g. what one ``send`` produced."""
    return [call.text for call in calls if call.text is not None]
