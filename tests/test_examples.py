"""The shop example, driven like a user would: the examples must keep working."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from vitrine.testing import TestClient, sent_texts

SHOP = Path(__file__).resolve().parents[1] / "examples" / "shop"


@pytest.fixture(scope="session")
def shop_modules():
    # imported once: callback prefixes are registered process-wide
    sys.path.insert(0, str(SHOP))
    try:
        return importlib.import_module("main"), importlib.import_module("botapp.cbs")
    finally:
        sys.path.remove(str(SHOP))


@pytest.fixture
def shop(shop_modules):
    main, cbs = shop_modules
    bot = main.build_bot()  # fresh services and state for every test
    bot.token = "123:TEST"
    return bot, cbs


def plain(calls) -> str:
    return " ".join(sent_texts(calls)).replace("\\", "")


async def test_a_purchase_from_browse_to_confirmation(shop):
    bot, cbs = shop
    async with TestClient(bot) as client:
        assert "The Shop" in plain(await client.send("/start"))
        assert "Catalog" in plain(await client.press(cbs.MenuCB(section="shop")))
        await client.press(cbs.ProductCB(sku="vpn30"))
        assert "How many" in plain(await client.press(cbs.BuyCB(sku="vpn30")))
        assert "Confirm order" in plain(await client.send("2"))
        assert "Order placed" in plain(await client.press(cbs.ConfirmCB(yes=True)))


async def test_the_order_command_takes_its_sku_as_an_argument(shop):
    bot, _ = shop
    async with TestClient(bot) as client:
        assert "Usage" in plain(await client.send("/order"))
        assert "How many" in plain(await client.send("/order vpn30"))
        assert "Confirm order" in plain(await client.send("/skip"))


async def test_the_cancel_button_abandons_the_order(shop):
    bot, cbs = shop
    async with TestClient(bot) as client:
        await client.send("/order vpn30")
        assert "Cancelled" in plain(await client.press(cbs.CancelCB()))
        assert sent_texts(await client.send("3")) == []  # no run left to answer it
