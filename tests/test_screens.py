"""Screen value object + Delivery transitions (edit, reply, proactive, replace)."""

from __future__ import annotations

import pytest
from telegram.constants import KeyboardButtonStyle
from telegram.error import BadRequest

from vitrine.markdown import Md, bold
from vitrine.screens import Button, Photo, Screen, media_kind

from conftest import FakeQuery, make_message, make_update


def test_screen_is_a_value_object_no_update_needed():
    screen = Screen(
        text=Md().heading("Hi there!").line("choose ", bold("wisely")),
        keyboard=[
            [Button("Go", callback="x"), Button("Docs", url="https://example.com")]
        ],
    )
    text, parse_mode = screen.content()
    assert parse_mode == "MarkdownV2"
    assert text == "*Hi there\\!*\nchoose *wisely*"

    markup = screen.markup()
    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == "x"
    assert markup.inline_keyboard[0][1].url == "https://example.com"


def test_button_style_passes_through():
    markup = Screen(
        keyboard=[
            [
                Button("Confirm", callback="ok", style="success"),
                Button("Delete", callback="rm", style=KeyboardButtonStyle.DANGER),
                Button("Plain", callback="p"),
            ]
        ],
    ).markup()
    assert markup is not None
    row = markup.inline_keyboard[0]
    assert row[0].style == "success"
    assert row[1].style == "danger"
    assert row[2].style is None


def test_button_rejects_unknown_style():
    with pytest.raises(ValueError, match="unknown button style"):
        Button("Bad", callback="x", style="blue")  # type: ignore[arg-type]


def test_media_kind_detects_all_types():
    assert media_kind(make_message(text="plain")) is None
    assert media_kind(make_message(photo=True)) == "photo"
    assert media_kind(make_message(document=True)) == "document"


async def test_reply_when_not_a_callback(delivery, fake_bot):
    update = make_update(text="/start")
    await delivery.render(update, Screen(text="hello"))

    sends = fake_bot.calls_to("send_message")
    assert sends[0]["chat_id"] == 1 and sends[0]["text"] == "hello"


async def test_proactive_send_to_arbitrary_chat(delivery, fake_bot):
    await delivery.send(987654, Screen(text="heads up"))
    assert fake_bot.calls_to("send_message")[0]["chat_id"] == 987654


async def test_edit_in_place_for_callback(delivery, fake_bot):
    old = make_message(text="old")
    update = make_update(query=FakeQuery(data="x", message=old))
    await delivery.render(update, Screen(text="new"))

    edit = fake_bot.calls_to("edit_message_text")[0]
    assert edit["message_id"] == old.message_id and edit["text"] == "new"
    assert not fake_bot.calls_to("send_message")


async def test_fresh_forces_new_message(delivery, fake_bot):
    update = make_update(query=FakeQuery(data="x", message=make_message(text="old")))
    await delivery.render(update, Screen(text="new"), fresh=True)
    assert fake_bot.calls_to("send_message")
    assert not fake_bot.calls_to("edit_message_text")


async def test_not_modified_is_swallowed(delivery, fake_bot):
    old = make_message(text="same")
    fake_bot.fail_once("edit_message_text", BadRequest("Message is not modified"))

    result = await delivery.edit(old, Screen(text="same"))
    assert result is old
    assert not fake_bot.calls_to("send_message")  # no replacement attempted


async def test_text_to_media_sends_before_deleting(delivery, fake_bot):
    old = make_message(text="text era")
    await delivery.edit(old, Screen(text="caption", media=Photo(b"png-bytes")))

    methods = [name for name, _ in fake_bot.calls]
    assert methods == ["send_photo", "delete_message"]  # replacement first, delete last
    assert fake_bot.calls_to("delete_message")[0]["message_id"] == old.message_id
    assert fake_bot.calls_to("send_photo")[0]["caption"] == "caption"


async def test_media_to_text_sends_before_deleting(delivery, fake_bot):
    old = make_message(photo=True)
    await delivery.edit(old, Screen(text="back to text"))

    methods = [name for name, _ in fake_bot.calls]
    assert methods == ["send_message", "delete_message"]


async def test_failed_replacement_send_keeps_old_message(delivery, fake_bot):
    old = make_message(text="precious")
    fake_bot.fail_once("send_photo", BadRequest("boom"))
    try:
        await delivery.edit(old, Screen(media=Photo(b"x")))
    except BadRequest:
        pass

    assert not fake_bot.calls_to(
        "delete_message"
    )  # never deleted without a replacement


async def test_media_to_media_edits_in_place(delivery, fake_bot):
    old = make_message(photo=True)
    await delivery.edit(old, Screen(text="cap", media=Photo(b"new-bytes")))

    assert fake_bot.calls_to("edit_message_media")
    assert not fake_bot.calls_to("delete_message")


async def test_file_id_cached_and_reused(delivery, fake_bot):
    photo = Photo(b"same-bytes")
    await delivery.send(1, Screen(media=photo))
    await delivery.send(2, Screen(media=photo))

    first, second = fake_bot.calls_to("send_photo")
    assert isinstance(first["photo"], bytes)
    assert isinstance(second["photo"], str) and second["photo"].startswith("fid-")


async def test_rejected_file_id_triggers_one_reupload(delivery, fake_bot):
    photo = Photo(b"payload")
    await delivery.send(1, Screen(media=photo))  # populates the cache
    fake_bot.fail_once(
        "send_photo", BadRequest("Wrong file identifier/HTTP URL specified")
    )
    await delivery.send(1, Screen(media=photo))

    sends = fake_bot.calls_to("send_photo")
    assert len(sends) == 3
    assert isinstance(sends[1]["photo"], str)  # tried the cached id
    assert isinstance(sends[2]["photo"], bytes)  # re-uploaded the source

    # and the cache learned the fresh id: a fourth send uses a file_id again
    await delivery.send(1, Screen(media=photo))
    assert isinstance(fake_bot.calls_to("send_photo")[3]["photo"], str)


async def test_edit_falls_back_to_replace_on_hard_failure(delivery, fake_bot):
    old = make_message(text="old")
    fake_bot.fail_once("edit_message_text", BadRequest("Message can't be edited"))
    await delivery.edit(old, Screen(text="new"))

    methods = [name for name, _ in fake_bot.calls]
    assert methods == ["edit_message_text", "send_message", "delete_message"]
