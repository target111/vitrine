"""Views: pure functions from domain objects to Screens.

No Update, no context, no I/O — every one of these is unit-testable by
calling it and inspecting the returned Screen.
"""

from __future__ import annotations

from vitrine import Button, Page, Screen, nav_row
from vitrine.markdown import Md, bold, code, italic

from domain.models import Order, OrderStatus, Product, User

from .cbs import BuyCB, ConfirmCB, MenuCB, OrdersPageCB, ProductCB, UsersPageCB

STATUS_ICONS = {OrderStatus.PENDING: "⏳", OrderStatus.PAID: "✅", OrderStatus.CANCELLED: "✖️"}


def home(user: User) -> Screen:
    text = (
        Md()
        .heading("🏬 The Shop")
        .line("Welcome back, ", bold(user.name), "!")
        .kv("Balance", code(f"{user.balance:.2f} cr"))
    )
    if user.is_admin:
        text.line(italic("You are an admin — see /help for admin commands."))

    return Screen(
        text=text,
        keyboard=[
            [Button("🛒 Browse", callback=MenuCB(section="shop"))],
            [
                Button("📦 My orders", callback=OrdersPageCB()),
                Button("💰 Wallet", callback=MenuCB(section="wallet")),
            ],
        ],
    )


def catalog(products: list[Product]) -> Screen:
    doc = Md().heading("🛒 Catalog").line(italic("Pick a product:"))
    keyboard = [
        [Button(f"{p.title} — {p.price:.2f} cr", callback=ProductCB(sku=p.sku))]
        for p in products
    ]
    keyboard.append([Button("« Home", callback=MenuCB(section="home"))])

    return Screen(text=doc, keyboard=keyboard)


def product_view(product: Product, user: User) -> Screen:
    text = (
        Md()
        .heading(product.title)
        .line(product.blurb)
        .blank()
        .kv("Price", code(f"{product.price:.2f} cr"))
        .kv("Your balance", code(f"{user.balance:.2f} cr"))
    )

    return Screen(
        text=text,
        keyboard=[
            [Button("🛍 Buy", callback=BuyCB(sku=product.sku))],
            [Button("« Catalog", callback=MenuCB(section="shop"))],
        ],
    )


def wallet(user: User) -> Screen:
    return Screen(
        text=Md().heading("💰 Wallet").kv("Balance", code(f"{user.balance:.2f} cr")),
        keyboard=[[Button("« Home", callback=MenuCB(section="home"))]],
    )


def ask_qty(product: Product) -> Screen:
    return Screen(
        text=Md()
        .line("How many ", bold(product.title), " would you like?")
        .line(italic("Send a number, or /cancel."))
    )


def confirm_order(product: Product, qty: int, total: float) -> Screen:
    return Screen(
        text=Md()
        .heading("Confirm order")
        .kv("Product", product.title)
        .kv("Quantity", code(qty))
        .kv("Total", code(f"{total:.2f} cr")),
        keyboard=[
            [
                Button("✅ Confirm", callback=ConfirmCB(yes=True)),
                Button("✖️ Abort", callback=ConfirmCB(yes=False)),
            ]
        ],
    )


def order_placed(order: Order, product: Product) -> Screen:
    return Screen(
        text=Md()
        .heading("⏳ Order placed")
        .line(bold(product.title), " ×", code(order.qty), " for ", code(f"{order.total:.2f} cr"))
        .line(italic("You'll get a message when payment confirms.")),
        keyboard=[[Button("« Home", callback=MenuCB(section="home"))]],
    )


def order_confirmed(order: Order, product: Product) -> Screen:
    """Pushed proactively by the reconciler worker."""
    return Screen(
        text=Md()
        .heading("✅ Payment confirmed")
        .line("Order ", code(f"#{order.id}"), " — ", bold(product.title), " ×", code(order.qty))
        .line("Thanks for your purchase!"),
        keyboard=[[Button("📦 My orders", callback=OrdersPageCB())]],
    )


def orders_page(page: Page[Order], products: dict[str, Product]) -> Screen:
    doc = Md().heading("📦 Your orders")
    if not page.items:
        doc.line(italic("Nothing here yet."))
    for order in page.items:
        title = products[order.sku].title if order.sku in products else order.sku
        doc.bullet(
            STATUS_ICONS[order.status],
            " ",
            code(f"#{order.id}"),
            f" {title} ×{order.qty} — {order.total:.2f} cr",
        )

    return Screen(
        text=doc,
        keyboard=[
            nav_row(page, lambda n: OrdersPageCB(page=n)),
            [Button("« Home", callback=MenuCB(section="home"))],
        ],
    )


def users_page(page: Page[User]) -> Screen:
    doc = Md().heading("👥 Users")
    for user in page.items:
        flags = "".join(("👑" if user.is_admin else "", "🚫" if user.banned else ""))
        doc.bullet(code(user.tg_id), f" {user.name} {flags}", f" — {user.balance:.2f} cr")

    return Screen(text=doc, keyboard=[nav_row(page, lambda n: UsersPageCB(page=n))])
