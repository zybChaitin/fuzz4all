"""Inventory reservation and transfer operations."""

from __future__ import annotations

from dataclasses import dataclass


class InsufficientInventoryError(RuntimeError):
    pass


@dataclass
class Reservation:
    order_id: str
    sku: str
    quantity: int


class InventoryService:
    def __init__(self, initial_stock: dict[str, int] | None = None) -> None:
        self._stock = dict(initial_stock or {})
        self._reservations: dict[str, list[Reservation]] = {}

    def available(self, sku: str) -> int:
        return self._stock.get(sku, 0)

    def reserve(self, order_id: str, sku: str, quantity: int) -> Reservation:
        available = self.available(sku)
        if available < quantity:
            raise InsufficientInventoryError(
                f"requested {quantity} units of {sku}, only {available} available"
            )

        self._stock[sku] = available - quantity
        reservation = Reservation(order_id=order_id, sku=sku, quantity=quantity)
        self._reservations.setdefault(order_id, []).append(reservation)
        return reservation

    def release_order(self, order_id: str) -> None:
        reservations = self._reservations.pop(order_id, [])
        for reservation in reservations:
            self._stock[reservation.sku] = (
                self.available(reservation.sku) + reservation.quantity
            )

    def transfer(self, source_sku: str, target_sku: str, quantity: int) -> None:
        source_available = self.available(source_sku)
        if source_available < quantity:
            raise InsufficientInventoryError(source_sku)
        self._stock[source_sku] = source_available - quantity
        self._stock[target_sku] = self.available(target_sku) + quantity

    def reservations_for_order(self, order_id: str) -> list[Reservation]:
        return self._reservations.get(order_id, [])
