"""Callback data: encoding, validation, and safe failure at dispatch."""

from __future__ import annotations

import pytest
from conftest import FakeQuery, make_context, make_dispatch, make_update

from vitrine.callbacks import CallbackData, decode
from vitrine.dispatch import EXPIRED_BUTTON_TEXT
from vitrine.exceptions import CallbackDataError
from vitrine.routing import Registration


class MenuCB(CallbackData, prefix="t_menu"):
    section: str
    page: int = 1
    flag: bool = False


class OtherCB(CallbackData, prefix="t_other"):
    value: str | None = None


def test_roundtrip():
    original = MenuCB(section="shop", page=3, flag=True)
    packed = original.pack()
    assert len(packed.encode()) <= 64
    assert MenuCB.unpack(packed) == original


def test_defaults_omitted_and_restored():
    packed = MenuCB(section="home").pack()
    decoded = MenuCB.unpack(packed)
    assert decoded.page == 1 and decoded.flag is False


def test_separator_and_spaces_survive_quoting():
    original = OtherCB(value="a:b c/d")
    assert OtherCB.unpack(original.pack()).value == "a:b c/d"


def test_none_roundtrip():
    assert OtherCB.unpack(OtherCB(value=None).pack()).value is None


def test_global_decode_registry():
    packed = MenuCB(section="x").pack()
    assert isinstance(decode(packed), MenuCB)
    with pytest.raises(CallbackDataError):
        decode("unknown:1:2")


def test_oversize_rejected():
    with pytest.raises(CallbackDataError):
        OtherCB(value="x" * 100).pack()


def test_wrong_prefix_and_bad_types_rejected():
    with pytest.raises(CallbackDataError):
        MenuCB.unpack("t_other:1")
    with pytest.raises(CallbackDataError):
        MenuCB.unpack("t_menu:shop:not-an-int")
    with pytest.raises(CallbackDataError):
        MenuCB.unpack("t_menu:shop:2:1:extra")


def test_prefix_collision_rejected():
    with pytest.raises(CallbackDataError):

        class Duplicate(CallbackData, prefix="t_menu"):
            pass


def test_matches():
    assert MenuCB.matches("t_menu:shop:1:0")
    assert MenuCB.matches("t_menu")
    assert not MenuCB.matches("t_menuX:1")
    assert not MenuCB.matches(None)


async def test_malformed_data_fails_safely_at_dispatch(fake_bot):
    """A stale/corrupt button answers politely; the handler never runs."""
    calls = []

    async def handler(data: MenuCB):
        calls.append(data)

    reg = Registration(kind="callback", fn=handler, name="menu", cb_model=MenuCB)
    query = FakeQuery(data="t_menu:shop:BOOM")
    update = make_update(query=query)

    dispatch = make_dispatch(fake_bot)
    result = await dispatch.run(reg, update, make_context(fake_bot))

    assert result is None
    assert calls == []
    assert query.answers[0][0] == EXPIRED_BUTTON_TEXT


async def test_decoded_data_injected(fake_bot):
    seen = []

    async def handler(data: MenuCB, update, context):
        seen.append(data)

    reg = Registration(kind="callback", fn=handler, name="menu", cb_model=MenuCB)
    query = FakeQuery(data=MenuCB(section="shop", page=2).pack())
    await dispatch_run(fake_bot, reg, query)
    assert seen == [MenuCB(section="shop", page=2)]


async def dispatch_run(fake_bot, reg, query):
    dispatch = make_dispatch(fake_bot)
    return await dispatch.run(reg, make_update(query=query), make_context(fake_bot))


# ------------------------------------------------------------- keyed encoding


class KeyedCB(CallbackData, prefix="t_keyed", keyed=True):
    section: str
    page: int = 1
    flag: bool = False


def test_keyed_roundtrip_and_wire_format():
    original = KeyedCB(section="shop", page=3, flag=True)
    packed = original.pack()
    assert packed == "t_keyed?section=shop&page=3&flag=1"
    assert KeyedCB.unpack(packed) == original


def test_keyed_omits_defaults():
    assert KeyedCB(section="home").pack() == "t_keyed?section=home"
    decoded = KeyedCB.unpack("t_keyed?section=home")
    assert decoded.page == 1 and decoded.flag is False


def test_keyed_survives_field_reordering_and_unknown_keys():
    # pairs in any order, plus a key from a since-removed field: still decodes
    decoded = KeyedCB.unpack("t_keyed?flag=1&legacy_field=9&section=shop")
    assert decoded == KeyedCB(section="shop", flag=True)


def test_keyed_values_survive_quoting():
    original = KeyedCB(section="a&b=c?d:e f")
    assert KeyedCB.unpack(original.pack()).section == "a&b=c?d:e f"


def test_keyed_rejects_malformed_and_bad_types():
    with pytest.raises(CallbackDataError):
        KeyedCB.unpack("t_keyed?section")  # no '='
    with pytest.raises(CallbackDataError):
        KeyedCB.unpack("t_keyed?page=NaN")  # bad type (and section missing)
    with pytest.raises(CallbackDataError):
        KeyedCB.unpack("t_keyed?page=2")  # required field absent
    with pytest.raises(CallbackDataError):
        KeyedCB.unpack("t_keyed?section=")  # empty -> None, not valid for str


def test_keyed_model_still_decodes_positional_data():
    """Flipping keyed=True must not strand buttons already in the wild."""
    assert KeyedCB.unpack("t_keyed:shop:2:1") == KeyedCB(section="shop", page=2, flag=True)


def test_positional_model_decodes_keyed_data():
    assert MenuCB.unpack("t_menu?section=shop&page=2") == MenuCB(section="shop", page=2)


def test_keyed_matches_and_global_decode():
    packed = KeyedCB(section="x").pack()
    assert KeyedCB.matches(packed) and KeyedCB.matches("t_keyed")
    assert not KeyedCB.matches("t_keyedX?section=1")
    assert isinstance(decode(packed), KeyedCB)


def test_keyed_oversize_rejected():
    with pytest.raises(CallbackDataError):
        KeyedCB(section="x" * 80).pack()


def test_prefix_cannot_contain_wire_characters():
    for bad in ("a?b", "a&b", "a=b", "a:b"):
        with pytest.raises(CallbackDataError):

            class Bad(CallbackData, prefix=bad):
                pass


def test_empty_string_field_rejected_at_pack_time():
    """"" and omitted look identical on the wire; packing one must fail loudly."""
    with pytest.raises(CallbackDataError, match="empty string"):
        MenuCB(section="").pack()
    with pytest.raises(CallbackDataError, match="empty string"):
        KeyedCB(section="").pack()


def test_keyed_inherited_from_abstract_base():
    class KeyedBase(CallbackData, keyed=True):
        pass

    class Child(KeyedBase, prefix="t_kchild"):
        item: str = "x"

    assert Child(item="y").pack() == "t_kchild?item=y"
    assert Child().pack() == "t_kchild"
    assert Child.unpack("t_kchild") == Child()
