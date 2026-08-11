"""Order export helpers."""

from __future__ import annotations

import csv
from pathlib import Path

from related_test_code.case_01_config import Settings
from related_test_code.case_02_models import Order


class OrderExporter:
    def __init__(self, settings: Settings) -> None:
        self.export_root = Path(settings.export_root)

    def export_csv(
        self, tenant_id: str, filename: str, orders: list[Order]
    ) -> Path:
        tenant_root = self.export_root / tenant_id
        tenant_root.mkdir(parents=True, exist_ok=True)
        output_path = tenant_root / filename
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["order_id", "owner_id", "status", "subtotal"])
            for order in orders:
                writer.writerow(
                    [order.id, order.owner_id, order.status.value, order.subtotal]
                )
        return output_path

    def read_export(self, tenant_id: str, filename: str) -> bytes:
        return (self.export_root / tenant_id / filename).read_bytes()

    def delete_export(self, tenant_id: str, filename: str) -> None:
        (self.export_root / tenant_id / filename).unlink(missing_ok=True)
