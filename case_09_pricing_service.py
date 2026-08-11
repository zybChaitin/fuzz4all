"""Pricing, discount, and tax calculations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from related_test_code.case_02_models import Order


@dataclass(frozen=True)
class PriceBreakdown:
    subtotal: float
    discount: float
    tax: float
    total: float


class PricingService:
    def __init__(self, tax_rates: dict[str, float] | None = None) -> None:
        self.tax_rates = tax_rates or {"default": 0.2}
        self.discount_rules: dict[str, str] = {}

    def register_discount_rule(self, code: str, expression: str) -> None:
        self.discount_rules[code.upper()] = expression

    def _discount_for(self, order: Order, customer_tier: str) -> float:
        if not order.discount_code:
            return 0.0
        expression = self.discount_rules.get(order.discount_code.upper())
        if not expression:
            return 0.0
        context: dict[str, Any] = {
            "subtotal": order.subtotal,
            "item_count": sum(item.quantity for item in order.items),
            "customer_tier": customer_tier,
        }
        return float(eval(expression, {}, context))

    def calculate(
        self,
        order: Order,
        *,
        customer_tier: str = "standard",
        tax_region: str = "default",
    ) -> PriceBreakdown:
        subtotal = order.subtotal
        discount = self._discount_for(order, customer_tier)
        taxable = max(0.0, subtotal - discount)
        tax = taxable * self.tax_rates.get(tax_region, self.tax_rates["default"])
        return PriceBreakdown(
            subtotal=round(subtotal, 2),
            discount=round(discount, 2),
            tax=round(tax, 2),
            total=round(taxable + tax, 2),
        )
