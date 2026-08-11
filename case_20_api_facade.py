"""Application facade connecting authentication, orders, and payments."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

from related_test_code.case_01_config import Settings
from related_test_code.case_02_models import (
    Order,
    OrderItem,
    PaymentRequest,
    RequestContext,
)
from related_test_code.case_03_auth_tokens import decode_token
from related_test_code.case_05_tenant_context import context_from_headers
from related_test_code.case_06_user_repository import UserRepository
from related_test_code.case_10_order_service import OrderService
from related_test_code.case_11_payment_gateway import PaymentGateway
from related_test_code.case_12_payment_retry import charge_with_retry
from related_test_code.case_16_audit_log import AuditLogger
from related_test_code.case_17_admin_service import AdminService
from related_test_code.case_19_serialization import serialize_order, serialize_payment


class CommerceApi:
    def __init__(
        self,
        settings: Settings,
        orders: OrderService,
        audit: AuditLogger,
    ) -> None:
        self.settings = settings
        self.orders = orders
        self.audit = audit
        connection = sqlite3.connect(settings.database_path, check_same_thread=False)
        self.users = UserRepository(connection)
        self.admin = AdminService(self.users)
        self.payments = PaymentGateway(settings)

    def authenticate(self, headers: Mapping[str, str]) -> RequestContext:
        authorization = headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            return decode_token(authorization.removeprefix("Bearer "), self.settings)
        return context_from_headers(headers)

    def create_order(
        self,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> str:
        context = self.authenticate(headers)
        raw_items = payload.get("items", [])
        items = [
            OrderItem(
                sku=str(item["sku"]),
                quantity=int(item["quantity"]),
                unit_price=float(item["unit_price"]),
            )
            for item in raw_items
            if isinstance(item, Mapping)
        ]
        order = self.orders.create_order(
            context,
            items,
            discount_code=str(payload.get("discount_code") or "") or None,
        )
        self.audit.record(
            context,
            "order.created",
            {"order": serialize_order(order), "headers": dict(headers)},
        )
        return serialize_order(order)

    def get_order(self, headers: Mapping[str, str], order_id: str) -> str:
        context = self.authenticate(headers)
        order = self.orders.get_order(context, order_id)
        return serialize_order(order)

    def submit_order(self, headers: Mapping[str, str], order_id: str) -> str:
        context = self.authenticate(headers)
        order = self.orders.submit(context, order_id)
        self.audit.record(context, "order.submitted", {"order_id": order.id})
        return serialize_order(order)

    def pay_order(
        self,
        headers: Mapping[str, str],
        order_id: str,
        card_token: str,
        return_url: str,
    ) -> str:
        context = self.authenticate(headers)
        order = self.orders.get_order(context, order_id)
        quote = self.orders.quote(
            context,
            order_id,
            customer_tier=headers.get("X-Customer-Tier", "standard"),
            tax_region=headers.get("X-Tax-Region", "default"),
        )
        payment = PaymentRequest(
            order_id=order.id,
            tenant_id=order.tenant_id,
            amount=quote.total,
            card_token=card_token,
            return_url=return_url,
            idempotency_key=order.id,
        )
        result = charge_with_retry(self.payments, payment)
        if result.status == "paid":
            self.orders.mark_paid(context, order.id, result.transaction_id)
        self.audit.record(
            context,
            "payment.completed",
            {"request": payment, "result": result.raw_response},
        )
        return serialize_payment(result)

    def cancel_order(self, headers: Mapping[str, str], order_id: str) -> Order:
        context = self.authenticate(headers)
        return self.orders.cancel(context, order_id)

    def promote_user(
        self,
        headers: Mapping[str, str],
        user_id: str,
        role: str,
    ) -> None:
        context = self.authenticate(headers)
        self.admin.promote_user(context, user_id, role)
        self.audit.record(
            context,
            "user.role_changed",
            {"target_user": user_id, "new_role": role},
        )
