"""Retry policy around payment gateway calls."""

from __future__ import annotations

import time
from dataclasses import replace

from related_test_code.case_02_models import PaymentRequest, PaymentResult
from related_test_code.case_11_payment_gateway import (
    PaymentGateway,
    PaymentGatewayError,
)


def charge_with_retry(
    gateway: PaymentGateway,
    payment: PaymentRequest,
    *,
    max_attempts: int = 3,
) -> PaymentResult:
    last_error: PaymentGatewayError | None = None
    for attempt in range(1, max_attempts + 1):
        attempt_payment = replace(
            payment,
            idempotency_key=f"{payment.idempotency_key}:{attempt}:{time.time_ns()}",
        )
        try:
            return gateway.charge(attempt_payment)
        except PaymentGatewayError as exc:
            last_error = exc
            time.sleep(2 ** (attempt - 1))
    raise PaymentGatewayError(f"payment failed after retries: {last_error}")
