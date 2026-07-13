"""Reply keyboards: value objects, delivery rules, and @reply_button routing."""

from __future__ import annotations

import pytest
from conftest import FakeQuery, make_message, make_update
from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove, Update

from vitrine.routing import Router
from vitrine.screens import (
    REMOVE_REPLY_KEYBOARD,
    Button,
    ReplyButton,
    ReplyKeyboard,
    Screen,
)


def test_reply_keyboard_builds_markup_from_strings_and_buttons():
    kb = ReplyKeyboard(
        [["🛍 Shop", ReplyButton("📞 Share number", request_contact=True)], ["ℹ️ Help"]],
        placeholder="Where to?",
    )
    markup = kb.to_ptb()

    assert isinstance(markup, ReplyKeyboardMarkup)
    assert markup.keyboard[0][0].text == "🛍 Shop"
    assert markup.keyboard[0][1].request_contact is True
    assert markup.keyboard[1][0].text == "ℹ️ Help"
    assert markup.is_persistent is True  # launcher-friendly defaults
    assert markup.resize_keyboard is True
    assert markup.one_time_keyboard is False
    assert markup.input_field_placeholder == "Where to?"


def test_screen_reply_markup_prefers_the_right_keyboard():
    launcher = ReplyKeyboard([["A"]])
    assert isinstance(Screen(reply_keyboard=launcher).reply_markup(), ReplyKeyboardMarkup)
    assert isinstance(
        Screen(reply_keyboard=REMOVE_REPLY_KEYBOARD).reply_markup(), ReplyKeyboardRemove
    )
    inline = Screen(keyboard=[[Button("Go", callback="x")]]).reply_markup()
    assert inline is not None and inline.inline_keyboard[0][0].callback_data == "x"


def test_inline_and_reply_keyboard_on_one_screen_is_an_error():
    screen = Screen(
        keyboard=[[Button("Go", callback="x")]],
        reply_keyboard=ReplyKeyboard([["A"]]),
    )
    with pytest.raises(ValueError, match="not both"):
        screen.reply_markup()


async def test_send_carries_the_reply_keyboard(delivery, fake_bot):
    await delivery.send(1, Screen(text="hi", reply_keyboard=ReplyKeyboard([["A"]])))

    markup = fake_bot.calls_to("send_message")[0]["reply_markup"]
    assert isinstance(markup, ReplyKeyboardMarkup)
    assert markup.keyboard[0][0].text == "A"


async def test_remove_reply_keyboard_passes_through(delivery, fake_bot):
    await delivery.send(1, Screen(text="bye", reply_keyboard=REMOVE_REPLY_KEYBOARD))

    markup = fake_bot.calls_to("send_message")[0]["reply_markup"]
    assert isinstance(markup, ReplyKeyboardRemove)


async def test_edit_into_reply_keyboard_screen_replaces(delivery, fake_bot):
    """Telegram can't attach reply keyboards via edit_*: send first, then delete."""
    old = make_message(text="old")
    update = make_update(query=FakeQuery(data="x", message=old))
    await delivery.render(update, Screen(text="new", reply_keyboard=ReplyKeyboard([["A"]])))

    methods = [name for name, _ in fake_bot.calls]
    assert methods == ["send_message", "delete_message"]
    assert fake_bot.calls_to("delete_message")[0]["message_id"] == old.message_id


def _tg_update(text: str) -> Update:
    return Update(update_id=1, message=make_message(text=text))


def test_reply_button_registers_an_exact_text_message_handler():
    router = Router()

    @router.reply_button("🛍 Shop", ReplyButton("👤 Profile"))
    async def open_screen():
        pass

    (reg,) = list(router.walk())
    assert reg.kind == "message"
    assert reg.filters.check_update(_tg_update("🛍 Shop"))
    assert reg.filters.check_update(_tg_update("👤 Profile"))
    assert not reg.filters.check_update(_tg_update("🛍 Shopping"))


def test_reply_button_requires_a_label():
    with pytest.raises(ValueError, match="at least one"):
        Router().reply_button()
