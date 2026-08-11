"""JSON serialization helpers for API responses."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any

from related_test_code.case_02_models import Order, PaymentResult, User


def _default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported type: {type(value).__name__}")


def serialize_user(user: User) -> str:
    return json.dumps(asdict(user), default=_default)


def serialize_order(order: Order) -> str:
    return json.dumps(asdict(order), default=_default)


def serialize_payment(result: PaymentResult) -> str:
    return json.dumps(asdict(result), default=_default)
