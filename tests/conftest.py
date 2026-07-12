"""Test fakes: no network, no live PTB application.

Real ``telegram`` objects (Message, Chat, User, PhotoSize) are used as plain
data; a recording FakeBot stands in for the API client.
"""

from __future__ import annotations

import datetime
import itertools
from types import SimpleNamespace
from typing import Any

import pytest
from telegram import Chat, Message, PhotoSize, User
from telegram import Document as TgDocument

from vitrine.dispatch import Dispatch
from vitrine.errors import ErrorRegistry
from vitrine.injection import Providers
from vitrine.media import InMemoryFileIdCache
from vitrine.ratelimit import RateLimiter
from vitrine.screens import Delivery

_ids = itertools.count(100)
_file_ids = itertools.count(1)


def make_chat(chat_id: int = 1) -> Chat:
    return Chat(id=chat_id, type="private")


def make_message(
    chat_id: int = 1,
    *,
    text: str | None = None,
    photo: bool = False,
    document: bool = False,
) -> Message:
    kwargs: dict[str, Any] = {}
    if photo:
        kwargs["photo"] = (
            PhotoSize(file_id=f"fid-{next(_file_ids)}", file_unique_id="u", width=1, height=1),
        )
    if document:
        kwargs["document"] = TgDocument(file_id=f"fid-{next(_file_ids)}", file_unique_id="u")

    return Message(
        message_id=next(_ids),
        date=datetime.datetime.now(datetime.UTC),
        chat=make_chat(chat_id),
        text=text,
        **kwargs,
    )


class FakeBot:
    """Records every API call; canned Message results; injectable failures."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._failures: dict[str, list[Exception]] = {}

    def fail_once(self, method: str, exc: Exception) -> None:
        self._failures.setdefault(method, []).append(exc)

    def calls_to(self, method: str) -> list[dict[str, Any]]:
        return [kwargs for name, kwargs in self.calls if name == method]

    def _hit(self, method: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((method, kwargs))
        queue = self._failures.get(method)
        if queue:
            raise queue.pop(0)

    async def send_message(self, **kwargs: Any) -> Message:
        self._hit("send_message", kwargs)
        return make_message(kwargs.get("chat_id", 1), text=kwargs.get("text"))

    async def send_photo(self, **kwargs: Any) -> Message:
        self._hit("send_photo", kwargs)
        return make_message(kwargs.get("chat_id", 1), photo=True)

    async def send_document(self, **kwargs: Any) -> Message:
        self._hit("send_document", kwargs)
        return make_message(kwargs.get("chat_id", 1), document=True)

    async def edit_message_text(self, **kwargs: Any) -> Message:
        self._hit("edit_message_text", kwargs)
        return make_message(kwargs.get("chat_id", 1), text=kwargs.get("text"))

    async def edit_message_media(self, **kwargs: Any) -> Message:
        self._hit("edit_message_media", kwargs)
        return make_message(kwargs.get("chat_id", 1), photo=True)

    async def delete_message(self, **kwargs: Any) -> bool:
        self._hit("delete_message", kwargs)
        return True

    async def set_my_commands(self, *args: Any, **kwargs: Any) -> bool:
        self._hit("set_my_commands", {"args": args, **kwargs})
        return True


class FakeQuery:
    def __init__(self, data: str | None = None, message: Message | None = None) -> None:
        self.data = data
        self.message = message
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text: str | None = None, show_alert: bool = False) -> None:
        self.answers.append((text, show_alert))


class FakeMessage(SimpleNamespace):
    """For paths that call reply_text (error UX); plain data otherwise."""

    def __init__(self, text: str | None = None, chat_id: int = 1) -> None:
        super().__init__(text=text, chat_id=chat_id)
        self.replies: list[tuple[str, dict[str, Any]]] = []

    async def reply_text(self, text: str, **kwargs: Any) -> None:
        self.replies.append((text, kwargs))


def make_update(
    *,
    user_id: int = 42,
    chat_id: int = 1,
    text: str | None = None,
    query: FakeQuery | None = None,
    message: Any = None,
) -> Any:
    """Returns a duck-typed stand-in for ``telegram.Update``, not a real one:
    handlers under test only touch a handful of attributes, and the dispatch
    layer treats updates as ``Any`` throughout."""
    if message is None and text is not None:
        message = FakeMessage(text=text, chat_id=chat_id)

    return SimpleNamespace(
        update_id=next(_ids),
        effective_user=User(id=user_id, first_name="Test", is_bot=False),
        effective_chat=make_chat(chat_id),
        effective_message=message,
        callback_query=query,
    )


def make_context(bot: FakeBot | None = None) -> SimpleNamespace:
    return SimpleNamespace(bot=bot or FakeBot(), bot_data={}, chat_data={}, user_data={})


@pytest.fixture
def fake_bot() -> FakeBot:
    return FakeBot()


@pytest.fixture
def delivery(fake_bot: FakeBot) -> Delivery:
    return Delivery(fake_bot, InMemoryFileIdCache())


@pytest.fixture
def providers() -> Providers:
    return Providers()


def make_dispatch(
    fake_bot: FakeBot,
    providers: Providers | None = None,
    auth: Any = None,
    middlewares: list | None = None,
) -> Dispatch:
    dispatch = Dispatch(
        providers or Providers(),
        ErrorRegistry(),
        RateLimiter(),
        auth,
        middlewares or [],
    )
    dispatch.delivery = Delivery(fake_bot, InMemoryFileIdCache())

    return dispatch


@pytest.fixture
def dispatch(fake_bot: FakeBot, providers: Providers) -> Dispatch:
    return make_dispatch(fake_bot, providers)
