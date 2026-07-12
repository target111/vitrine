"""The pipeline: args, middleware, throttling, screen returns, error UX."""

from __future__ import annotations

from conftest import FakeQuery, make_context, make_dispatch, make_message, make_update

from vitrine.args import Greedy
from vitrine.exceptions import UserFacingError
from vitrine.routing import Registration
from vitrine.screens import Screen


async def test_command_args_parsed_and_injected(fake_bot):
    seen = {}

    async def pay(update, amount: float, note: Greedy = Greedy("-")):
        seen.update(amount=amount, note=note)

    reg = Registration(kind="command", fn=pay, name="pay", command="pay")
    dispatch = make_dispatch(fake_bot)
    await dispatch.run(
        reg, make_update(text="/pay 2.5 thanks for lunch"), make_context(fake_bot)
    )

    assert seen == {"amount": 2.5, "note": "thanks for lunch"}


async def test_usage_message_on_bad_args(fake_bot):
    async def pay(update, amount: int):
        raise AssertionError("must not run")

    reg = Registration(kind="command", fn=pay, name="pay", command="pay")
    dispatch = make_dispatch(fake_bot)
    update = make_update(text="/pay nope")
    await dispatch.run(reg, update, make_context(fake_bot))

    reply, kwargs = update.effective_message.replies[0]
    assert "/pay" in reply and "amount" in reply


async def test_returned_screen_is_rendered(fake_bot):
    async def start(update):
        return Screen(text="welcome")

    reg = Registration(kind="command", fn=start, name="start", command="start")
    dispatch = make_dispatch(fake_bot)
    await dispatch.run(reg, make_update(text="/start"), make_context(fake_bot))

    assert fake_bot.calls_to("send_message")[0]["text"] == "welcome"


async def test_screen_return_edits_for_callback_updates(fake_bot):
    async def view(update):
        return Screen(text="edited view")

    reg = Registration(kind="message", fn=view, name="view")
    old = make_message(text="original")
    update = make_update(query=FakeQuery(data="x", message=old))
    await make_dispatch(fake_bot).run(reg, update, make_context(fake_bot))

    assert fake_bot.calls_to("edit_message_text")[0]["message_id"] == old.message_id


async def test_middleware_wraps_and_extras_are_injectable(fake_bot):
    order = []

    async def outer(event, call_next):
        order.append("outer>")
        event.extras["locale"] = "en"
        result = await call_next(event)
        order.append("<outer")
        return result

    async def inner(event, call_next):
        order.append("inner>")
        return await call_next(event)

    async def handler(update, locale):
        order.append(f"handler:{locale}")

    reg = Registration(kind="message", fn=handler, name="h", middlewares=[inner])
    dispatch = make_dispatch(fake_bot, middlewares=[outer])
    await dispatch.run(reg, make_update(text="x"), make_context(fake_bot))

    assert order == ["outer>", "inner>", "handler:en", "<outer"]


async def test_middleware_sees_handler_name_and_data(fake_bot):
    from test_callbacks import MenuCB

    seen = {}

    async def mw(event, call_next):
        seen["name"] = event.handler_name
        seen["data"] = event.data
        return await call_next(event)

    async def handler(data):
        pass

    reg = Registration(
        kind="callback", fn=handler, name="menu", cb_model=MenuCB, middlewares=[mw]
    )
    query = FakeQuery(data=MenuCB(section="s").pack())
    await make_dispatch(fake_bot).run(reg, make_update(query=query), make_context(fake_bot))

    assert seen == {"name": "menu", "data": MenuCB(section="s")}


async def test_throttle_limits_and_reports(fake_bot):
    from vitrine.ratelimit import throttle

    ran = []

    @throttle(2, per=60)
    async def spammy(update):
        ran.append(1)

    reg = Registration(kind="command", fn=spammy, name="spammy", command="spammy")
    dispatch = make_dispatch(fake_bot)
    context = make_context(fake_bot)
    updates = [make_update(text="/spammy") for _ in range(3)]
    for update in updates:
        await dispatch.run(reg, update, context)

    assert len(ran) == 2
    reply, _ = updates[2].effective_message.replies[0]
    assert "Slow down" in reply

    # a different user is unaffected
    other = make_update(user_id=999, text="/spammy")
    await dispatch.run(reg, other, context)

    assert len(ran) == 3


async def test_user_facing_error_becomes_alert_on_callback(fake_bot):
    from test_callbacks import MenuCB

    async def handler(data):
        raise UserFacingError("Insufficient funds", show_alert=True)

    reg = Registration(kind="callback", fn=handler, name="wallet", cb_model=MenuCB)
    query = FakeQuery(data=MenuCB(section="wallet").pack())
    await make_dispatch(fake_bot).run(reg, make_update(query=query), make_context(fake_bot))

    assert ("Insufficient funds", True) in query.answers


async def test_custom_error_handler_renders_into_current_screen(fake_bot):
    class Broke(Exception):
        pass

    dispatch = make_dispatch(fake_bot)

    @dispatch.errors.on(Broke)
    async def broke_ux(error, update):
        return Screen(text="friendly failure")

    async def handler(update):
        raise Broke()

    reg = Registration(kind="message", fn=handler, name="h")
    old = make_message(text="menu")
    update = make_update(query=FakeQuery(data="x", message=old))
    await dispatch.run(reg, update, make_context(fake_bot))

    edit = fake_bot.calls_to("edit_message_text")[0]
    assert edit["text"] == "friendly failure" and edit["message_id"] == old.message_id


async def test_mro_dispatch_most_specific_wins(fake_bot):
    class Base(Exception):
        pass

    class Specific(Base):
        pass

    hits = []
    dispatch = make_dispatch(fake_bot)

    @dispatch.errors.on(Base)
    async def base_ux(error):
        hits.append("base")

    @dispatch.errors.on(Specific)
    async def specific_ux(error):
        hits.append("specific")

    async def handler(update):
        raise Specific()

    reg = Registration(kind="message", fn=handler, name="h")
    await dispatch.run(reg, make_update(text="x"), make_context(fake_bot))

    assert hits == ["specific"]


async def test_unexpected_errors_never_reach_the_user_raw(fake_bot):
    async def handler(update):
        raise RuntimeError("secret internals")

    reg = Registration(kind="message", fn=handler, name="h")
    update = make_update(text="x")
    await make_dispatch(fake_bot).run(reg, update, make_context(fake_bot))

    reply, _ = update.effective_message.replies[0]
    assert "secret internals" not in reply
    assert "went wrong" in reply
