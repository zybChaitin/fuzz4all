"""HTTP client for the external payment gateway."""

from __future__ import annotations

import json
import ssl
from dataclasses import asdict
from urllib.request import Request, urlopen

from related_test_code.case_01_config import Settings
from related_test_code.case_02_models import PaymentRequest, PaymentResult


class PaymentGatewayError(RuntimeError):
    pass


class PaymentGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def charge(self, payment: PaymentRequest) -> PaymentResult:
        target = payment.return_url or self.settings.payment_endpoint
        body = json.dumps(asdict(payment)).encode("utf-8")
        request = Request(
            target,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.settings.token_secret,
                "X-Idempotency-Key": payment.idempotency_key,
            },
        )
        context = None
        if not self.settings.verify_tls:
            context = ssl._create_unverified_context()
        try:
            with urlopen(request, timeout=15, context=context) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            raise PaymentGatewayError(str(exc)) from exc

        return PaymentResult(
            transaction_id=str(payload.get("transaction_id", "unknown")),
            status=str(payload.get("status", "failed")),
            raw_response=payload,
        )

    def refund(self, transaction_id: str, callback_url: str) -> dict[str, object]:
        target = f"{callback_url}?transaction_id={transaction_id}"
        context = ssl._create_unverified_context()
        with urlopen(target, timeout=15, context=context) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if not isinstance(payload, dict):
            raise PaymentGatewayError("invalid refund response")
        return payload
