"""The DI annotation lint: report only what a subclass test can settle.

Injection is by name, so an annotation is documentation, not a contract --
duck-typed stand-ins stay legal, and everything the lint cannot *prove*
(protocols, generics, unions, unannotated factories, TYPE_CHECKING-only
names) is left alone. What it can prove becomes a ``vitrine.build`` warning,
or a build failure under ``Bot(strict_types=True)``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Protocol

import pytest
from conftest import make_dispatch

from vitrine import Bot, Depends
from vitrine.exceptions import ConfigurationError
from vitrine.injection import Providers, annotation_conflicts
from vitrine.routing import Registration

if TYPE_CHECKING:
    from decimal import Decimal  # a name that only exists for the type checker


def conflicts(fn, providers: Providers) -> list[str]:
    return annotation_conflicts(fn, providers)


def test_a_value_provider_that_provably_disagrees_is_reported():
    providers = Providers()
    providers.register_value("count", "3")

    async def handler(update, count: int): ...

    (finding,) = conflicts(handler, providers)
    assert "count" in finding and "int" in finding and "str" in finding


def test_a_factory_return_annotation_that_disagrees_is_reported():
    providers = Providers()

    def make_count() -> str:
        return "3"

    providers.register("count", make_count)

    async def handler(update, count: int): ...

    (finding,) = conflicts(handler, providers)
    assert "count" in finding


def test_an_async_factory_is_checked_on_its_return_type():
    providers = Providers()

    async def make_count() -> str:
        return "3"

    providers.register("count", make_count)

    async def handler(update, count: int): ...

    assert conflicts(handler, providers)


def test_an_async_generator_is_checked_on_what_it_yields():
    """The yielded type is what reaches the handler, not the generator."""
    providers = Providers()

    async def session() -> AsyncIterator[str]:
        yield "sess"

    providers.register("session", session)

    async def handler(update, session: int): ...

    assert conflicts(handler, providers)

    async def ok(update, session: str): ...

    assert not conflicts(ok, providers)


def test_an_explicit_depends_is_checked_too():
    async def make_token() -> str:
        return "tok"

    async def handler(update, token: int = Depends(make_token)): ...

    (finding,) = conflicts(handler, Providers())
    assert "token" in finding and "Depends" in finding


def test_int_under_a_float_annotation_is_fine():
    """The one implicit conversion Python makes."""
    providers = Providers()
    providers.register_value("amount", 3)
    providers.register("rate", lambda: 2)

    async def handler(update, amount: float, rate: float): ...

    # the lambda has no annotation (skipped); the value is an int (allowed)
    assert not conflicts(handler, providers)

    def make_rate() -> int:
        return 2

    providers.register("rate", make_rate)
    assert not conflicts(handler, providers)


def test_a_duck_typed_value_matching_the_class_is_fine():
    class Repo:
        pass

    class SubRepo(Repo):
        pass

    providers = Providers()
    providers.register_value("repo", SubRepo())

    async def handler(update, repo: Repo): ...

    assert not conflicts(handler, providers)


def test_what_a_subclass_test_cannot_settle_is_left_alone():
    class Closeable(Protocol):
        def close(self) -> None: ...

    providers = Providers()
    providers.register_value("conn", object())  # satisfies nothing provably
    providers.register_value("items", [1, 2])
    providers.register_value("value", "x")
    providers.register("thing", lambda: 3)  # unannotated factory

    async def handler(
        update,
        conn: Closeable,  # protocol
        items: list[int],  # generic
        value: int | bytes,  # union
        thing: str,  # factory with no return annotation
        mystery: Decimal = None,  # TYPE_CHECKING-only name
    ): ...

    assert conflicts(handler, providers) == []


def test_reserved_names_and_command_args_are_not_linted(fake_bot):
    """`update: Update` and typed command arguments are converted or supplied
    by the framework; they are none of the lint's business."""
    dispatch = make_dispatch(fake_bot)

    async def pay(update, amount: float): ...

    reg = Registration(kind="command", fn=pay, name="pay", command="pay")
    dispatch.validate(reg)  # no ConfigurationError, nothing to report


def test_the_lint_reports_on_the_build_logger(fake_bot, caplog):
    providers = Providers()
    providers.register_value("count", "3")
    dispatch = make_dispatch(fake_bot, providers=providers)

    async def handler(update, count: int): ...

    reg = Registration(kind="message", fn=handler, name="h")
    with caplog.at_level("WARNING", logger="vitrine.build"):
        dispatch.validate(reg)

    assert any("count" in record.getMessage() for record in caplog.records)


def test_strict_types_turns_the_warning_into_a_build_failure():
    bot = Bot(token="123:TEST", strict_types=True)
    bot.provide_value("count", "3")

    @bot.message()
    async def handler(update, count: int): ...

    with pytest.raises(ConfigurationError, match="count"):
        bot._wire_handlers()


def test_without_strict_types_the_app_still_builds():
    bot = Bot(token="123:TEST")
    bot.provide_value("count", "3")

    @bot.message()
    async def handler(update, count: int): ...

    bot._wire_handlers()  # warning only
