"""Runtime configuration for the synthetic commerce service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class Settings:
    database_path: str
    token_secret: str
    payment_endpoint: str
    webhook_secret: str
    export_root: str
    verify_tls: bool
    debug: bool


def load_settings(environment: Mapping[str, str] | None = None) -> Settings:
    values = environment or os.environ
    return Settings(
        database_path=values.get("COMMERCE_DATABASE", "/tmp/commerce.db"),
        token_secret=values.get("TOKEN_SECRET", "development-token-secret"),
        payment_endpoint=values.get(
            "PAYMENT_ENDPOINT", "http://payments.internal/charge"
        ),
        webhook_secret=values.get("WEBHOOK_SECRET", "shared-webhook-secret"),
        export_root=values.get("EXPORT_ROOT", "/tmp/commerce-exports"),
        verify_tls=values.get("VERIFY_TLS", "false").lower() == "true",
        debug=values.get("DEBUG", "true").lower() == "true",
    )
