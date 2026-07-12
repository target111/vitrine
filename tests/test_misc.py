"""Markdown builder, command args, pagination, rate limiter internals."""

from __future__ import annotations

import pytest

from vitrine.args import ArgSpec, Greedy, build_arg_specs, parse_args, usage_string
from vitrine.exceptions import UsageError
from vitrine.markdown import Md, bold, code, escape, italic, link, raw
from vitrine.pagination import ListSource, Paginator, nav_row
from vitrine.ratelimit import RateLimiter
from vitrine.screens import NOOP

# ------------------------------------------------------------------- markdown


def test_escaping_v2_and_v1():
    assert escape("a_b*c[d]e.f!", 2) == r"a\_b\*c\[d\]e\.f\!"
    assert escape("a_b*c[d.e!", 1) == r"a\_b\*c\[d.e!"


def test_user_input_cannot_break_markup():
    evil = "*bold* _inj_ [x](http://e.vil)"
    rendered = Md().line(bold("Hello ", evil)).render(2)
    assert rendered == r"*Hello \*bold\* \_inj\_ \[x\]\(http://e\.vil\)*"


def test_nesting_bold_link_and_lists():
    doc = Md().bullet(bold(link("Order #7", "https://x.y/o?a=1&b=2")), " — ", italic("paid"))
    assert doc.render(2) == "• *[Order \\#7](https://x.y/o?a=1&b=2)* — _paid_"


def test_code_and_raw_escape_hatch():
    assert code("a`b\\c").render(2) == "`a\\`b\\\\c`"
    assert raw("*prerendered*").render(2) == "*prerendered*"


def test_v1_fallback_drops_unsupported_styles():
    from vitrine.markdown import spoiler

    assert spoiler("secret").render(1) == "secret"
    assert spoiler("secret").render(2) == "||secret||"


# ------------------------------------------------------------------- args


def sample(update, amount: int, target: str = "self", note: Greedy = Greedy("")):
    pass


def specs():
    return build_arg_specs(sample, skip={"update"})


def test_specs_and_usage():
    assert [s.name for s in specs()] == ["amount", "target", "note"]
    assert usage_string("pay", specs()) == "/pay <amount> [target] [note...]"


def test_parse_required_optional_greedy():
    values = parse_args("pay", specs(), "5 alice for the pizza last night")
    assert values == {
        "amount": 5,
        "target": "alice",
        "note": "for the pizza last night",
    }

    values = parse_args("pay", specs(), "5")
    assert values == {"amount": 5, "target": "self", "note": ""}


def test_parse_failures():
    with pytest.raises(UsageError, match="missing amount"):
        parse_args("pay", specs(), "")
    with pytest.raises(UsageError, match="integer"):
        parse_args("pay", specs(), "abc")
    no_greedy = [ArgSpec("amount", int, True, False)]
    with pytest.raises(UsageError, match="too many"):
        parse_args("pay", no_greedy, "1 2")


def test_bool_conversion():
    spec = [ArgSpec("flag", bool, True, False)]
    assert parse_args("t", spec, "yes") == {"flag": True}
    assert parse_args("t", spec, "off") == {"flag": False}


# ------------------------------------------------------------------- pagination


async def test_paginator_fetches_only_the_page():
    fetched = []

    class Source:
        async def count(self):
            return 23

        async def fetch(self, offset, limit):
            fetched.append((offset, limit))
            return list(range(offset, min(offset + limit, 23)))

    paginator = Paginator(Source(), per_page=10)
    page = await paginator.page(2)

    assert fetched == [(10, 10)]
    assert page.number == 2 and page.pages == 3 and page.has_prev and page.has_next


async def test_page_clamping_and_list_source():
    paginator = Paginator(ListSource(list("abcdefgh")), per_page=3)
    page = await paginator.page(99)
    assert page.number == 3 and list(page.items) == ["g", "h"]

    page = await paginator.page(-5)
    assert page.number == 1


async def test_nav_row_buttons():
    paginator = Paginator(ListSource(list(range(30))), per_page=10)
    page = await paginator.page(2)
    row = nav_row(page, lambda n: f"pg:{n}")
    assert [b.callback for b in row] == ["pg:1", NOOP, "pg:3"]

    first = nav_row(await paginator.page(1), lambda n: f"pg:{n}")
    assert first[0].callback == NOOP and first[2].callback == "pg:2"


# ------------------------------------------------------------------- ratelimit


def test_sliding_window():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    assert limiter.check("k", 2, per=10) == 0.0
    assert limiter.check("k", 2, per=10) == 0.0
    assert limiter.check("k", 2, per=10) == pytest.approx(10.0)

    now[0] = 10.1
    assert limiter.check("k", 2, per=10) == 0.0
