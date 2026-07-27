"""Guided purchase conversation: Buy button -> quantity -> confirm.

Two ways in -- the Buy button on a product, or /order <sku> typed directly --
and both leave the flow in the same state. ``exclusive=True`` means starting
one order abandons whatever half-finished order the caller still had open,
instead of letting it swallow the next number they send.
"""

from __future__ import annotations

from dataclasses import dataclass

from domain.models import User
from domain.services import CatalogService, OrderService

from vitrine import ANY_STATE, END, Conversation, ExitReason, Screen
from vitrine.logging import audit

from . import views
from .cbs import BuyCB, CancelCB, ConfirmCB


@dataclass
class OrderDraft:
    sku: str = ""
    qty: int = 0


order_flow = Conversation("order", OrderDraft, timeout=180, exclusive=True)


@order_flow.entry(callback=BuyCB)
async def begin(state: OrderDraft, data: BuyCB, catalog: CatalogService):
    state.sku = data.sku
    product = await catalog.get(data.sku)

    return "qty", views.ask_qty(product)


@order_flow.entry(command="order", description="Order a product by its SKU")
async def begin_by_command(state: OrderDraft, update, catalog: CatalogService):
    """An entry command is a real command: /order shows up in /help and in the
    Telegram command menu, exactly like one declared with @router.command."""
    # Steps are not command handlers, so they get no typed args -- read the
    # text. END rather than None: a run that never really started is over, so
    # the draft is cleared instead of lingering until the next order.
    sku = (update.effective_message.text or "").partition(" ")[2].strip()
    if not sku:
        return END, Screen(text="Send /order <sku> — see /start for the catalog.")

    # An unknown SKU raises UnknownProduct, which the DomainError handler in
    # main.py turns into a screen. The step never returns a state, so no run
    # starts -- the caller just gets the error and can try again.
    product = await catalog.get(sku)
    state.sku = sku

    return "qty", views.ask_qty(product)


@order_flow.state("qty", command="skip")
async def skip_qty(state: OrderDraft, catalog: CatalogService):
    """/skip takes the default of one. Valid only while the run sits in "qty",
    so it stays out of /help — it would do nothing outside the flow."""
    state.qty = 1
    product = await catalog.get(state.sku)

    return "confirm", views.confirm_order(product, 1, product.price)


@order_flow.state(ANY_STATE, callback=CancelCB)
async def cancel_button():
    """One handler, every state: the Cancel button works wherever it is shown.

    A step needs no parameters at all if it uses none. Note this ends the run
    as FINISHED, not CANCELLED -- only the /cancel fallback reports the latter.
    """
    return END, Screen(text="Cancelled — nothing was charged.")


@order_flow.state("qty")
async def got_qty(state: OrderDraft, update, catalog: CatalogService):
    text = (update.effective_message.text or "").strip()
    if not text.isdigit() or not 0 < int(text) <= 50:
        return None, Screen(text="Please send a number between 1 and 50, or /cancel.")

    state.qty = int(text)
    product = await catalog.get(state.sku)

    return "confirm", views.confirm_order(product, state.qty, product.price * state.qty)


@order_flow.state("confirm", callback=ConfirmCB)
async def confirm(
    state: OrderDraft,
    data: ConfirmCB,
    update,
    user: User,
    orders: OrderService,
    catalog: CatalogService,
):
    if not data.yes:
        return END, Screen(text="Order aborted — nothing was charged.")

    product = await catalog.get(state.sku)
    order = await orders.place(user, update.effective_chat.id, state.sku, state.qty)
    audit("order.placed", actor=user.tg_id, order=order.id, total=order.total)

    return END, views.order_placed(order, product)


@order_flow.cancel()
async def cancel(update):
    await update.effective_message.reply_text("Cancelled — nothing was charged.")


@order_flow.on_exit
async def on_exit(state: OrderDraft, reason: ExitReason):
    if reason is not ExitReason.FINISHED:
        audit("order.abandoned", sku=state.sku, reason=reason.value)
