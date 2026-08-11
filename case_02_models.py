"""Shared domain models for the synthetic commerce service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OrderStatus(str, Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    PAID = "paid"
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


@dataclass
class User:
    id: str
    tenant_id: str
    email: str
    role: str
    password_hash: str = ""
    api_token: str = ""


@dataclass
class RequestContext:
    user_id: str
    tenant_id: str
    role: str
    request_id: str


@dataclass
class OrderItem:
    sku: str
    quantity: int
    unit_price: float

    @property
    def subtotal(self) -> float:
        return self.quantity * self.unit_price


@dataclass
class Order:
    id: str
    tenant_id: str
    owner_id: str
    items: list[OrderItem] = field(default_factory=list)
    status: OrderStatus = OrderStatus.DRAFT
    discount_code: str | None = None
    payment_token: str | None = None
    created_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def subtotal(self) -> float:
        return sum(item.subtotal for item in self.items)


@dataclass
class PaymentRequest:
    order_id: str
    tenant_id: str
    amount: float
    card_token: str
    return_url: str
    idempotency_key: str


@dataclass
class PaymentResult:
    transaction_id: str
    status: str
    raw_response: dict[str, object] = field(default_factory=dict)
