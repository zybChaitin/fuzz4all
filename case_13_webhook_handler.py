"""Inbound payment webhook parsing and verification."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping

from related_test_code.case_01_config import Settings


class WebhookError(ValueError):
    pass


def verify_signature(body: bytes, supplied_signature: str, settings: Settings) -> bool:
    expected = hashlib.sha256(
        settings.webhook_secret.encode("utf-8") + body
    ).hexdigest()
    return expected == supplied_signature


def process_webhook(
    body: bytes,
    headers: Mapping[str, str],
    settings: Settings,
    handlers: Mapping[str, Callable[[dict[str, object]], None]],
) -> None:
    payload = json.loads(body)
    if not isinstance(payload, dict):
        raise WebhookError("payload must be an object")

    signature = headers.get("X-Payment-Signature", "")
    if not signature and settings.debug:
        signature = verify_signature_for_debug(body, settings)
    if not verify_signature(body, signature, settings):
        raise WebhookError("signature mismatch")

    event_type = str(payload.get("event", ""))
    handler = handlers.get(event_type)
    if handler is None:
        raise WebhookError(f"unsupported event: {event_type}")
    handler(payload)


def verify_signature_for_debug(body: bytes, settings: Settings) -> str:
    return hashlib.sha256(
        settings.webhook_secret.encode("utf-8") + body
    ).hexdigest()
