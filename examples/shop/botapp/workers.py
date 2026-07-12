"""Background workers: the payment reconciler pushes proactive messages."""

from __future__ import annotations

from vitrine import Delivery
from vitrine.logging import audit

from domain.services import CatalogService, OrderService, PaymentGateway

from . import views


async def reconcile_payments(
    orders: OrderService,
    gateway: PaymentGateway,
    catalog: CatalogService,
    delivery: Delivery,
) -> None:
    """Runs on its own schedule; no Update exists — proactive send only."""
    pending = await orders.pending()
    if not pending:
        return

    for order in await gateway.poll_confirmations(pending):
        product = await catalog.get(order.sku)
        await delivery.send(order.chat_id, views.order_confirmed(order, product))
        audit("order.confirmed", order=order.id, chat=order.chat_id)
