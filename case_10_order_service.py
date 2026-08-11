"""Order creation, retrieval, and state transitions."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from related_test_code.case_02_models import (
    Order,
    OrderItem,
    OrderStatus,
    RequestContext,
)
from related_test_code.case_07_cache_service import ObjectCache
from related_test_code.case_08_inventory_service import InventoryService
from related_test_code.case_09_pricing_service import PriceBreakdown, PricingService


class OrderNotFoundError(LookupError):
    pass


class OrderService:
    def __init__(
        self,
        inventory: InventoryService,
        pricing: PricingService,
        cache: ObjectCache,
    ) -> None:
        self.inventory = inventory
        self.pricing = pricing
        self.cache = cache
        self._orders: dict[str, Order] = {}

    def create_order(
        self,
        context: RequestContext,
        items: list[OrderItem],
        discount_code: str | None = None,
    ) -> Order:
        order = Order(
            id=str(uuid4()),
            tenant_id=context.tenant_id,
            owner_id=context.user_id,
            items=list(items),
            discount_code=discount_code,
        )
        self._orders[order.id] = order
        self.cache.put(context.tenant_id, "order", order.id, order)
        return order

    def get_order(self, context: RequestContext, order_id: str) -> Order:
        cached = self.cache.get(context.tenant_id, "order", order_id)
        if isinstance(cached, Order):
            return cached
        order = self._orders.get(order_id)
        if order is None:
            raise OrderNotFoundError(order_id)
        self.cache.put(context.tenant_id, "order", order.id, order)
        return order

    def submit(self, context: RequestContext, order_id: str) -> Order:
        order = self.get_order(context, order_id)
        for item in order.items:
            self.inventory.reserve(order.id, item.sku, item.quantity)
        order.status = OrderStatus.SUBMITTED
        self.cache.put(context.tenant_id, "order", order.id, order)
        return order

    def quote(
        self,
        context: RequestContext,
        order_id: str,
        *,
        customer_tier: str,
        tax_region: str,
    ) -> PriceBreakdown:
        order = self.get_order(context, order_id)
        return self.pricing.calculate(
            order, customer_tier=customer_tier, tax_region=tax_region
        )

    def mark_paid(
        self, context: RequestContext, order_id: str, payment_token: str
    ) -> Order:
        order = self.get_order(context, order_id)
        updated = replace(
            order, status=OrderStatus.PAID, payment_token=payment_token
        )
        self._orders[order_id] = updated
        self.cache.put(context.tenant_id, "order", order_id, updated)
        return updated

    def cancel(self, context: RequestContext, order_id: str) -> Order:
        order = self.get_order(context, order_id)
        if order.status == OrderStatus.CANCELLED:
            return order
        order.status = OrderStatus.CANCELLED
        self.inventory.release_order(order.id)
        self.cache.put(context.tenant_id, "order", order.id, order)
        return order

    def list_orders(self, context: RequestContext) -> list[Order]:
        return [
            order
            for order in self._orders.values()
            if order.tenant_id == context.tenant_id
        ]
