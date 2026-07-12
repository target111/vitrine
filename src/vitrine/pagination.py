"""Pagination: count + fetch + nav buttons over a tiny source protocol.

Sources fetch only the requested page, so DB-backed sources stay cheap::

    class OrderSource:
        def __init__(self, repo, user_id): ...
        async def count(self) -> int: ...
        async def fetch(self, offset: int, limit: int) -> Sequence[Order]: ...

    class OrdersCB(CallbackData, prefix="ord"):
        page: int = 1

    page = await Paginator(OrderSource(repo, uid), per_page=5).page(data.page)
    screen = Screen(
        text=render_orders(page.items),
        keyboard=[nav_row(page, lambda n: OrdersCB(page=n))],
    )
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Protocol, Sequence, TypeVar

from .callbacks import CallbackData
from .screens import NOOP, Button

T = TypeVar("T")


class PageSource(Protocol[T]):
    """Anything paginatable: an in-memory list, a DB query, an API."""

    async def count(self) -> int: ...

    async def fetch(self, offset: int, limit: int) -> Sequence[T]: ...


class ListSource(Generic[T]):
    """In-memory source over any sequence."""

    def __init__(self, items: Sequence[T]) -> None:
        self._items = items

    async def count(self) -> int:
        return len(self._items)

    async def fetch(self, offset: int, limit: int) -> Sequence[T]:
        return self._items[offset : offset + limit]


@dataclass(frozen=True)
class Page(Generic[T]):
    items: Sequence[T]
    number: int  # 1-based
    per_page: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages


class Paginator(Generic[T]):
    def __init__(self, source: PageSource[T], per_page: int = 10) -> None:
        self.source = source
        self.per_page = per_page

    async def page(self, number: int = 1) -> Page[T]:
        """Fetch one page; out-of-range numbers are clamped, never an error."""
        total = await self.source.count()
        pages = max(1, -(-total // self.per_page))
        number = min(max(1, number), pages)
        items = await self.source.fetch((number - 1) * self.per_page, self.per_page)

        return Page(items=items, number=number, per_page=self.per_page, total=total)


def nav_row(
    page: Page,
    make_callback: Callable[[int], CallbackData | str],
    *,
    labels: tuple[str, str] = ("‹ Prev", "Next ›"),
    counter: bool = True,
) -> list[Button]:
    """Prev / counter / Next buttons; hidden edges become no-op fillers."""
    prev_label, next_label = labels
    row: list[Button] = []
    row.append(
        Button(prev_label, callback=make_callback(page.number - 1))
        if page.has_prev
        else Button(" ", callback=NOOP)
    )

    if counter:
        row.append(Button(f"{page.number}/{page.pages}", callback=NOOP))

    row.append(
        Button(next_label, callback=make_callback(page.number + 1))
        if page.has_next
        else Button(" ", callback=NOOP)
    )

    return row
