"""Markdown builder, command args, pagination, rate limiter internals, error UX."""

from __future__ import annotations

import deferred_annotations
import pytest
from conftest import FakeMessage, make_update

from vitrine.args import ArgSpec, Greedy, build_arg_specs, parse_args, usage_string
from vitrine.errors import ErrorRegistry
from vitrine.exceptions import ConfigurationError, UsageError
from vitrine.injection import Depends, Invocation, Providers
from vitrine.markdown import Md, bold, code, escape, italic, link, raw
from vitrine.pagination import ListSource, Paginator, nav_row
from vitrine.ratelimit import _SWEEP_EVERY, RateLimiter
from vitrine.routing import doc_body, doc_summary
from vitrine.screens import NOOP, Delivery

# ------------------------------------------------------------------- markdown


def test_escaping_v2_and_v1():
    assert escape("a_b*c[d]e.f!", 2) == r"a\_b\*c\[d\]e\.f\!"
    assert escape("a_b*c[d.e!", 1) == r"a\_b\*c\[d.e!"


def test_user_input_cannot_break_markup():
    evil = "*bold* _inj_ [x](http://e.vil)"
    rendered = Md().line(bold("Hello ", evil)).render(2)
    assert rendered == r"*Hello \*bold\* \_inj\_ \[x\]\(http://e\.vil\)*"


def test_nesting_bold_link_and_lists():
    doc = Md().bullet(
        bold(link("Order #7", "https://x.y/o?a=1&b=2")), " — ", italic("paid")
    )
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


def test_args_survive_an_unresolvable_injected_annotation():
    """Issue #1: `update: Update` under TYPE_CHECKING broke every other arg.

    The signature cannot be evaluated as a whole, so the real types have to be
    recovered per parameter -- and only for the ones that are arguments.
    """
    specs = build_arg_specs(deferred_annotations.note, skip={"update"})

    assert [(s.name, s.annotation) for s in specs] == [
        ("tag", str),
        ("amount", int),
        ("rest", Greedy),
    ]


def test_a_greedy_arg_stays_greedy_with_deferred_annotations():
    """The string 'Greedy' matched nothing, so the trailing param silently
    stopped consuming the rest of the line."""
    specs = build_arg_specs(deferred_annotations.note, skip={"update"})

    assert specs[-1].greedy
    assert parse_args("note", specs, "x 5 and the rest") == {
        "tag": "x",
        "amount": 5,
        "rest": "and the rest",
    }


def test_an_argument_type_must_be_importable_at_runtime():
    """Converting calls the type, so a TYPE_CHECKING-only one cannot work --
    say so at build time instead of rejecting every value the user sends."""
    with pytest.raises(ConfigurationError, match="target"):
        build_arg_specs(
            deferred_annotations.unresolvable_argument, skip={"update"}
        )


def test_a_depends_parameter_is_not_a_command_argument():
    """An explicit `Depends` default is injected, but no name registers it, so
    `skip` cannot carry it and it used to be parsed off the command line."""

    def grant(update, tg_id: int, service=Depends(Providers)):
        pass

    specs = build_arg_specs(grant, skip={"update"})

    assert [s.name for s in specs] == ["tg_id"]
    assert usage_string("grant", specs) == "/grant <tg_id>"
    with pytest.raises(UsageError, match="too many"):
        parse_args("grant", specs, "7 sneaks-into-service")


def test_doc_body_is_everything_after_the_summary():
    def documented():
        """Send credits.

        Cleared instantly.
            Indented, and kept that way.
        """

    def summary_only():
        """Send credits."""

    def blank():
        """
        """

    assert doc_body(documented) == "Cleared instantly.\n    Indented, and kept that way."
    assert doc_body(summary_only) == ""
    assert doc_body(blank) == ""
    assert doc_body(test_doc_body_is_everything_after_the_summary) == ""


def test_a_wrapped_summary_is_not_cut_at_the_line_break():
    """It is one sentence in the source and must stay one in the description --
    which goes to /help *and* to Telegram's command menu."""

    def wrapped():
        """Send credits to another user, clearing instantly and
        irreversibly.

        The note shows up on both statements.
        """

    assert doc_summary(wrapped) == (
        "Send credits to another user, clearing instantly and irreversibly."
    )
    assert doc_body(wrapped) == "The note shows up on both statements."


def test_doc_summary_edge_cases():
    def leading_newline():
        """
        Place an order.

        The rest is not the description.
        """

    def summary_only():
        """Check that the bot is alive."""

    def blank():
        """
        """

    assert doc_summary(leading_newline) == "Place an order."
    assert doc_summary(summary_only) == "Check that the bot is alive."
    assert doc_summary(blank) == ""
    assert doc_summary(test_doc_summary_edge_cases) == ""


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


def test_idle_keys_are_swept():
    now = [0.0]
    limiter = RateLimiter(clock=lambda: now[0])
    limiter.check("idle", 2, per=10)

    now[0] = 100.0
    for i in range(_SWEEP_EVERY):
        limiter.check(f"k{i}", 2, per=10)

    assert "idle" not in limiter._hits  # fully expired window: evicted
    assert "k0" in limiter._hits  # live windows survive the sweep


# ------------------------------------------------------------------- error UX


async def test_usage_error_ux_respects_markdown_version(fake_bot):
    inv = Invocation(
        update=make_update(message=FakeMessage()),
        delivery=Delivery(fake_bot, markdown_version=1),
    )
    await ErrorRegistry().dispatch(
        UsageError("/pay <amount>", hint="missing amount"), inv, Providers()
    )

    sent = fake_bot.calls_to("send_message")[0]
    assert sent["parse_mode"] == "Markdown"
    assert "missing amount" in sent["text"]
