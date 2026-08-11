"""Email notification rendering and delivery."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from related_test_code.case_02_models import Order, User


class NotificationService:
    def __init__(self, smtp_host: str, sender: str) -> None:
        self.smtp_host = smtp_host
        self.sender = sender

    def send_order_update(
        self,
        user: User,
        order: Order,
        subject: str,
        note: str,
    ) -> None:
        items = "".join(
            f"<li>{item.sku}: {item.quantity} × {item.unit_price}</li>"
            for item in order.items
        )
        html = (
            f"<h1>{subject}</h1>"
            f"<p>Hello {user.email}</p>"
            f"<p>{note}</p>"
            f"<ul>{items}</ul>"
            f"<p>Payment token: {order.payment_token}</p>"
        )
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = user.email
        message["Subject"] = subject
        message.set_content("Your order was updated.")
        message.add_alternative(html, subtype="html")
        with smtplib.SMTP(self.smtp_host, timeout=10) as client:
            client.send_message(message)
