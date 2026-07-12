"""Domain models. This package never imports the bot layer (or telegram)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


@dataclass
class User:
    """The rich principal used across every menu and guard."""

    id: int  # internal id
    tg_id: int
    name: str
    roles: set[str] = field(default_factory=set)
    banned: bool = False
    balance: float = 100.0  # demo credit

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles


@dataclass(frozen=True)
class Product:
    sku: str
    title: str
    price: float
    blurb: str


class OrderStatus(str, Enum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"


@dataclass
class Order:
    id: int
    user_id: int
    chat_id: int
    sku: str
    qty: int
    total: float
    status: OrderStatus = OrderStatus.PENDING
    created_at: float = field(default_factory=time.time)


class DomainError(Exception):
    """Base for domain failures; the bot layer maps these to UX."""


class InsufficientBalance(DomainError):
    def __init__(self, needed: float, available: float) -> None:
        super().__init__(f"need {needed:.2f}, have {available:.2f}")
        self.needed = needed
        self.available = available


class UnknownProduct(DomainError):
    pass
