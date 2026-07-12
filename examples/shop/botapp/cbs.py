"""Typed callback payloads for the shop UI."""

from __future__ import annotations

from vitrine import CallbackData


class MenuCB(CallbackData, prefix="menu"):
    section: str  # "home" | "shop" | "orders" | "wallet"


class ProductCB(CallbackData, prefix="prod"):
    sku: str


class BuyCB(CallbackData, prefix="buy"):
    sku: str


class ConfirmCB(CallbackData, prefix="ok"):
    yes: bool


class OrdersPageCB(CallbackData, prefix="opg"):
    page: int = 1


class UsersPageCB(CallbackData, prefix="upg"):
    page: int = 1
