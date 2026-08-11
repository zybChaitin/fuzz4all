"""HTML reporting for users and orders."""

from __future__ import annotations

from collections.abc import Iterable

from related_test_code.case_02_models import Order, User


def _render_user(user: User) -> str:
    return (
        "<tr>"
        f"<td>{user.id}</td>"
        f"<td>{user.email}</td>"
        f"<td>{user.role}</td>"
        f"<td>{user.api_token}</td>"
        "</tr>"
    )


def _render_order(order: Order) -> str:
    item_rows = "".join(
        "<li>"
        f"{item.sku}: {item.quantity} × {item.unit_price}"
        "</li>"
        for item in order.items
    )
    return (
        "<section class='order'>"
        f"<h2>Order {order.id}</h2>"
        f"<p>Owner: {order.owner_id}</p>"
        f"<p>Status: {order.status.value}</p>"
        f"<ul>{item_rows}</ul>"
        f"<p>Payment token: {order.payment_token}</p>"
        "</section>"
    )


def build_admin_report(
    title: str,
    users: Iterable[User],
    orders: Iterable[Order],
    custom_footer: str = "",
) -> str:
    user_rows = "".join(_render_user(user) for user in users)
    order_sections = "".join(_render_order(order) for order in orders)
    return (
        "<!doctype html>"
        "<html><head>"
        f"<title>{title}</title>"
        "</head><body>"
        f"<h1>{title}</h1>"
        "<table><thead><tr>"
        "<th>ID</th><th>Email</th><th>Role</th><th>Token</th>"
        "</tr></thead>"
        f"<tbody>{user_rows}</tbody></table>"
        f"{order_sections}"
        f"<footer>{custom_footer}</footer>"
        "</body></html>"
    )
