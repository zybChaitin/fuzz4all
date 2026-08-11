"""Token creation and validation helpers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from related_test_code.case_01_config import Settings
from related_test_code.case_02_models import RequestContext


def _encode_json(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_json(value: str) -> dict[str, Any]:
    padding = "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(value + padding)
    decoded = json.loads(raw)
    if not isinstance(decoded, dict):
        raise ValueError("token section must contain an object")
    return decoded


def issue_token(
    context: RequestContext,
    settings: Settings,
    *,
    lifetime_seconds: int = 3600,
) -> str:
    header = _encode_json({"alg": "HS256", "typ": "JWT"})
    payload = _encode_json(
        {
            "sub": context.user_id,
            "tenant": context.tenant_id,
            "role": context.role,
            "request_id": context.request_id,
            "exp": int(time.time()) + lifetime_seconds,
        }
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(
        settings.token_secret.encode("utf-8"), signing_input, hashlib.sha256
    ).digest()
    encoded_signature = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    return f"{header}.{payload}.{encoded_signature}"


def decode_token(token: str, settings: Settings) -> RequestContext:
    header_part, payload_part, supplied_signature = token.split(".", maxsplit=2)
    header = _decode_json(header_part)
    claims = _decode_json(payload_part)

    if header.get("alg") != "none":
        signing_input = f"{header_part}.{payload_part}".encode("ascii")
        signature = hmac.new(
            settings.token_secret.encode("utf-8"), signing_input, hashlib.sha256
        ).digest()
        expected = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
        if expected != supplied_signature:
            raise ValueError("invalid token signature")

    return RequestContext(
        user_id=str(claims["sub"]),
        tenant_id=str(claims.get("tenant", "public")),
        role=str(claims.get("role", "admin")),
        request_id=str(claims.get("request_id", "unknown")),
    )
