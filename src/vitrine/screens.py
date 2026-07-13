"""Screens: message-as-value-object, and Delivery: the one way messages go out.

A :class:`Screen` bundles text + keyboard + optional media + send options. It is
plain data — constructible and unit-testable with no ``Update`` and no I/O.

:class:`Delivery` turns screens into Telegram calls three ways:

- ``send(chat_id, screen)`` — proactive send to any chat (workers, notifications)
- ``edit(message, screen)`` — edit-in-place, robust across content-type changes
- ``render(update, screen)`` — the ergonomic path: edits when the update came
  from an inline button, replies otherwise

Robustness rules implemented here:

- on a text<->media (or non-editable media) transition the replacement is sent
  **before** the old message is deleted, so a failure never strands the user
- all existing media kinds are detected (photo, video, animation, document,
  audio, voice, video note, sticker), not just photos
- uploads are cached by content hash and re-sent as ``file_id``; a rejected
  ``file_id`` triggers exactly one re-upload and the cache is refreshed
- Telegram's "message is not modified" is swallowed
- ``fresh=True`` forces a brand-new message instead of an edit
- reply keyboards (:class:`ReplyKeyboard`) only ride on sends — Telegram cannot
  attach them via ``edit_*`` — so editing into a screen that carries one
  becomes a replace (new message first, then delete)
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaAnimation,
    InputMediaAudio,
    InputMediaDocument,
    InputMediaPhoto,
    InputMediaVideo,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import KeyboardButtonStyle
from telegram.error import BadRequest, TelegramError

from .callbacks import CallbackData
from .markdown import Node
from .media import FileIdCache, InMemoryFileIdCache, content_key

if TYPE_CHECKING:
    from telegram.ext import CallbackContext

logger = logging.getLogger("vitrine.delivery")

DELIVERY_KEY = "__vitrine_delivery__"

#: callback_data used by decorative buttons (page counters etc.); auto-answered
NOOP = "__noop__"


# --------------------------------------------------------------------------- keyboard

ButtonStyle = Literal["primary", "success", "danger"]
_BUTTON_STYLES = frozenset({"primary", "success", "danger"})


@dataclass(frozen=True)
class Button:
    """One inline button. Exactly one of ``callback``/``url`` should be set.

    ``style`` colors the button on clients that support it (Bot API styled
    buttons, PTB >= 22.7): ``"primary"`` (blue), ``"success"`` (green) or
    ``"danger"`` (red) — :class:`telegram.constants.KeyboardButtonStyle`
    members work too. Older clients ignore it.
    """

    text: str
    callback: CallbackData | str | None = None
    url: str | None = None
    style: ButtonStyle | KeyboardButtonStyle | None = None

    def __post_init__(self) -> None:
        if self.style is not None and str(self.style) not in _BUTTON_STYLES:
            raise ValueError(
                f"unknown button style {self.style!r}; "
                f"expected one of {sorted(_BUTTON_STYLES)}"
            )

    def to_ptb(self) -> InlineKeyboardButton:
        callback_data: str | None = None
        if self.callback is not None:
            callback_data = (
                self.callback.pack()
                if isinstance(self.callback, CallbackData)
                else self.callback
            )

        return InlineKeyboardButton(
            self.text, callback_data=callback_data, url=self.url, style=self.style
        )


Row = Sequence[Button | InlineKeyboardButton]
KeyboardLike = InlineKeyboardMarkup | Sequence[Row] | None


def build_markup(keyboard: KeyboardLike) -> InlineKeyboardMarkup | None:
    if keyboard is None:
        return None
    if isinstance(keyboard, InlineKeyboardMarkup):
        return keyboard

    rows = [
        [btn.to_ptb() if isinstance(btn, Button) else btn for btn in row]
        for row in keyboard
    ]
    if not rows:
        return None

    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- reply keyboard


@dataclass(frozen=True)
class ReplyButton:
    """One reply-keyboard button. Pressing it sends its ``text`` as a message.

    ``request_contact``/``request_location`` ask the client to share those
    instead of sending text; handle the resulting updates with
    ``@bot.message(filters.CONTACT)`` / ``filters.LOCATION`` — such presses
    never reach a ``@bot.reply_button`` handler.
    """

    text: str
    request_contact: bool = False
    request_location: bool = False

    def to_ptb(self) -> KeyboardButton:
        return KeyboardButton(
            self.text,
            request_contact=self.request_contact,
            request_location=self.request_location,
        )


ReplyRow = Sequence[ReplyButton | KeyboardButton | str]


@dataclass(frozen=True)
class ReplyKeyboard:
    """A reply keyboard shown under the input field, as a value object.

    Defaults are tuned for the launcher pattern: ``persistent=True`` keeps it
    visible across messages, ``resize=True`` avoids the giant-button look. Set
    it once (e.g. from ``/start``) via ``Screen(reply_keyboard=...)`` and wire
    the presses with ``@bot.reply_button(label)``. Send
    :data:`REMOVE_REPLY_KEYBOARD` to take it away again.
    """

    rows: Sequence[ReplyRow]
    persistent: bool = True
    resize: bool = True
    one_time: bool = False
    placeholder: str | None = None
    selective: bool = False

    def to_ptb(self) -> ReplyKeyboardMarkup:
        rows = [
            [
                btn.to_ptb()
                if isinstance(btn, ReplyButton)
                else KeyboardButton(btn)
                if isinstance(btn, str)
                else btn
                for btn in row
            ]
            for row in self.rows
        ]

        return ReplyKeyboardMarkup(
            rows,
            is_persistent=self.persistent,
            resize_keyboard=self.resize,
            one_time_keyboard=self.one_time,
            input_field_placeholder=self.placeholder,
            selective=self.selective,
        )


#: put on ``Screen.reply_keyboard`` to remove the user's current reply keyboard
REMOVE_REPLY_KEYBOARD = ReplyKeyboardRemove()

ReplyKeyboardLike = ReplyKeyboard | ReplyKeyboardMarkup | ReplyKeyboardRemove | None


# --------------------------------------------------------------------------- media

_EDITABLE_KINDS = frozenset({"photo", "video", "animation", "document", "audio"})
_CAPTION_KINDS = frozenset({"photo", "video", "animation", "document", "audio", "voice"})

_SEND_METHODS = {
    "photo": ("send_photo", "photo"),
    "video": ("send_video", "video"),
    "animation": ("send_animation", "animation"),
    "document": ("send_document", "document"),
    "audio": ("send_audio", "audio"),
    "voice": ("send_voice", "voice"),
    "video_note": ("send_video_note", "video_note"),
    "sticker": ("send_sticker", "sticker"),
}

_INPUT_MEDIA = {
    "photo": InputMediaPhoto,
    "video": InputMediaVideo,
    "animation": InputMediaAnimation,
    "document": InputMediaDocument,
    "audio": InputMediaAudio,
}

# animation must be checked before document: Telegram sets both on GIF messages
_DETECT_ORDER = (
    "animation",
    "photo",
    "video",
    "video_note",
    "sticker",
    "voice",
    "audio",
    "document",
)


@dataclass(frozen=True)
class Media:
    """A media attachment: a source (bytes / local path / URL) and/or a file_id."""

    kind: str
    source: bytes | str | Path | None = None
    file_id: str | None = None
    filename: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in _SEND_METHODS:
            raise ValueError(f"unknown media kind {self.kind!r}")
        if self.source is None and self.file_id is None:
            raise ValueError("Media needs a source or a file_id")

    def cache_key(self) -> str | None:
        return content_key(self.source) if self.source is not None else None


def Photo(source: bytes | str | Path | None = None, **kw: Any) -> Media:
    return Media("photo", source, **kw)


def Video(source: bytes | str | Path | None = None, **kw: Any) -> Media:
    return Media("video", source, **kw)


def Animation(source: bytes | str | Path | None = None, **kw: Any) -> Media:
    return Media("animation", source, **kw)


def Document(source: bytes | str | Path | None = None, **kw: Any) -> Media:
    return Media("document", source, **kw)


def Audio(source: bytes | str | Path | None = None, **kw: Any) -> Media:
    return Media("audio", source, **kw)


def Voice(source: bytes | str | Path | None = None, **kw: Any) -> Media:
    return Media("voice", source, **kw)


def media_kind(message: Message | None) -> str | None:
    """Detect the media kind of an existing message; ``None`` means text-only."""
    if message is None:
        return None

    for kind in _DETECT_ORDER:
        if getattr(message, kind, None):
            return kind
    return None


def _harvest_file_id(message: Message, kind: str) -> str | None:
    if kind == "photo":
        sizes = message.photo
        return sizes[-1].file_id if sizes else None

    obj = getattr(message, kind, None)
    return obj.file_id if obj is not None else None


def _is_not_modified(exc: BadRequest) -> bool:
    return "message is not modified" in str(exc).lower()


def _is_bad_file_id(exc: BadRequest) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "wrong file identifier",
            "wrong remote file",
            "file not found",
            "file reference",
            "wrong file_id",
        )
    )


# --------------------------------------------------------------------------- screen


@dataclass
class Screen:
    """A renderable message as a value object. No ``Update`` required to build one."""

    text: str | Node | None = None
    keyboard: KeyboardLike = None
    media: Media | None = None
    reply_keyboard: ReplyKeyboardLike = None
    parse_mode: str | None = None
    link_preview: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def content(self, markdown_version: int = 2) -> tuple[str | None, str | None]:
        """Resolve (text, parse_mode). Markdown nodes render with safe escaping."""
        if isinstance(self.text, Node):
            mode = "MarkdownV2" if markdown_version == 2 else "Markdown"
            return self.text.render(markdown_version), mode

        return self.text, self.parse_mode

    def markup(self) -> InlineKeyboardMarkup | None:
        return build_markup(self.keyboard)

    def reply_markup(
        self,
    ) -> InlineKeyboardMarkup | ReplyKeyboardMarkup | ReplyKeyboardRemove | None:
        """The effective ``reply_markup`` for a send: inline or reply keyboard."""
        inline = self.markup()
        if self.reply_keyboard is None:
            return inline
        if inline is not None:
            raise ValueError(
                "a message carries an inline keyboard or a reply keyboard, not both"
            )
        if isinstance(self.reply_keyboard, ReplyKeyboard):
            return self.reply_keyboard.to_ptb()

        return self.reply_keyboard

    async def render(
        self, update: Update, context: CallbackContext, *, fresh: bool = False
    ) -> Message:
        """Ergonomic shortcut: deliver via the bot's Delivery service."""
        delivery = context.bot_data.get(DELIVERY_KEY) or Delivery(context.bot)
        return await delivery.render(update, self, fresh=fresh)

    async def send_to(self, chat_id: int, context_or_delivery: Any) -> Message:
        """Proactively send this screen to an arbitrary chat id."""
        if isinstance(context_or_delivery, Delivery):
            delivery = context_or_delivery
        else:
            ctx = context_or_delivery
            delivery = ctx.bot_data.get(DELIVERY_KEY) or Delivery(ctx.bot)

        return await delivery.send(chat_id, self)


# --------------------------------------------------------------------------- delivery


class Delivery:
    """Sends, edits, and replaces screens. Injectable as ``delivery``."""

    def __init__(
        self,
        bot: Any,
        file_ids: FileIdCache | None = None,
        *,
        markdown_version: int = 2,
    ) -> None:
        self.bot = bot
        self.file_ids: FileIdCache = file_ids or InMemoryFileIdCache()
        self.markdown_version = markdown_version

    # -- public API ----------------------------------------------------------

    async def send(self, chat_id: int, screen: Screen, **extra: Any) -> Message:
        """Proactive send to any chat id (background workers use this)."""
        if screen.media is not None:
            return await self._send_media(chat_id, screen, **extra)

        return await self._send_text(chat_id, screen, **extra)

    async def render(
        self, update: Update, screen: Screen, *, fresh: bool = False
    ) -> Message:
        """Edit in place when the update came from an inline button, else reply."""
        query = update.callback_query
        message = query.message if query is not None else None
        if not fresh and isinstance(message, Message):
            return await self.edit(message, screen)

        chat = update.effective_chat
        if chat is None:
            raise ValueError("update has no chat to deliver to")

        return await self.send(chat.id, screen)

    async def edit(self, message: Message, screen: Screen, **extra: Any) -> Message:
        """Edit ``message`` into ``screen``, replacing it when Telegram can't edit.

        Replacement always sends the new message *before* deleting the old one.
        """
        if screen.reply_keyboard is not None:
            # Telegram can't attach reply keyboards via edit_*: replace instead.
            return await self._replace(message, screen)

        old_kind = media_kind(message)
        new_kind = screen.media.kind if screen.media is not None else None

        if old_kind is None and new_kind is None:
            return await self._edit_text(message, screen, **extra)
        if (
            old_kind in _EDITABLE_KINDS
            and new_kind in _EDITABLE_KINDS
            and screen.media is not None
        ):
            return await self._edit_media(message, screen, **extra)
        # text<->media or a non-editable kind on either side: replace
        return await self._replace(message, screen)

    # -- internals ------------------------------------------------------------

    def _text_kwargs(self, screen: Screen) -> dict[str, Any]:
        text, parse_mode = screen.content(self.markdown_version)
        return {"text": text or "", "parse_mode": parse_mode}

    async def _send_text(self, chat_id: int, screen: Screen, **extra: Any) -> Message:
        return await self.bot.send_message(
            chat_id=chat_id,
            reply_markup=screen.reply_markup(),
            link_preview_options=LinkPreviewOptions(is_disabled=not screen.link_preview),
            **self._text_kwargs(screen),
            **{**screen.extra, **extra},
        )

    async def _edit_text(self, message: Message, screen: Screen, **extra: Any) -> Message:
        try:
            result = await self.bot.edit_message_text(
                chat_id=message.chat_id,
                message_id=message.message_id,
                reply_markup=screen.markup(),
                link_preview_options=LinkPreviewOptions(
                    is_disabled=not screen.link_preview
                ),
                **self._text_kwargs(screen),
                **{**screen.extra, **extra},
            )
        except BadRequest as exc:
            if _is_not_modified(exc):
                return message
            logger.debug("edit_message_text failed (%s); replacing message", exc)
            return await self._replace(message, screen)
        return result if isinstance(result, Message) else message

    async def _media_input(self, media: Media) -> tuple[Any, str | None, bool]:
        """Resolve what to hand Telegram: (input, cache_key, used_cached_id)."""
        if media.file_id is not None:
            return media.file_id, None, False

        key = media.cache_key()
        if key is not None:
            cached = await self.file_ids.get(key)
            if cached is not None:
                return cached, key, True

        source = media.source
        if isinstance(source, Path):
            return source.read_bytes(), key, False

        return source, key, False  # bytes or URL string

    async def _remember(self, key: str | None, message: Message, kind: str) -> None:
        if key is None:
            return

        file_id = _harvest_file_id(message, kind)
        if file_id:
            await self.file_ids.set(key, file_id)

    @staticmethod
    def _fresh_bytes(media: Media) -> bytes | str | None:
        if isinstance(media.source, Path):
            return media.source.read_bytes()

        return media.source

    async def _send_media(self, chat_id: int, screen: Screen, **extra: Any) -> Message:
        media = screen.media
        assert media is not None
        method_name, param = _SEND_METHODS[media.kind]
        method = getattr(self.bot, method_name)
        kwargs: dict[str, Any] = {
            "chat_id": chat_id,
            "reply_markup": screen.reply_markup(),
            **screen.extra,
            **extra,
        }
        if media.kind in _CAPTION_KINDS:
            text, parse_mode = screen.content(self.markdown_version)
            kwargs["caption"] = text
            kwargs["parse_mode"] = parse_mode
        if media.filename:
            kwargs["filename"] = media.filename

        input_, key, used_cache = await self._media_input(media)
        try:
            message = await method(**{param: input_, **kwargs})
        except BadRequest as exc:
            if not _is_bad_file_id(exc) or media.source is None:
                raise
            # cached/explicit file_id rejected: re-upload once from the source
            if key is not None:
                await self.file_ids.discard(key)
            logger.info("file_id rejected (%s); re-uploading %s", exc, media.kind)
            message = await method(**{param: self._fresh_bytes(media), **kwargs})

        await self._remember(key, message, media.kind)
        return message

    async def _edit_media(self, message: Message, screen: Screen, **extra: Any) -> Message:
        media = screen.media
        assert media is not None
        input_media_cls = _INPUT_MEDIA[media.kind]
        text, parse_mode = screen.content(self.markdown_version)

        async def attempt(input_: Any) -> Message:
            result = await self.bot.edit_message_media(
                chat_id=message.chat_id,
                message_id=message.message_id,
                media=input_media_cls(media=input_, caption=text, parse_mode=parse_mode),
                reply_markup=screen.markup(),
                **{**screen.extra, **extra},
            )
            return result if isinstance(result, Message) else message

        input_, key, used_cache = await self._media_input(media)
        try:
            result = await attempt(input_)
        except BadRequest as exc:
            if _is_not_modified(exc):
                return message
            if _is_bad_file_id(exc) and media.source is not None:
                if key is not None:
                    await self.file_ids.discard(key)
                logger.info("file_id rejected on edit (%s); re-uploading", exc)
                try:
                    result = await attempt(self._fresh_bytes(media))
                except BadRequest as exc2:
                    logger.debug("edit_message_media retry failed (%s); replacing", exc2)
                    return await self._replace(message, screen)
            else:
                logger.debug("edit_message_media failed (%s); replacing message", exc)
                return await self._replace(message, screen)

        await self._remember(key, result, media.kind)
        return result

    async def _replace(self, message: Message, screen: Screen) -> Message:
        """Content-type transition: send the replacement first, then delete."""
        new_message = await self.send(message.chat_id, screen)
        try:
            await self.bot.delete_message(
                chat_id=message.chat_id, message_id=message.message_id
            )
        except TelegramError as exc:
            logger.debug("could not delete replaced message: %s", exc)

        return new_message
