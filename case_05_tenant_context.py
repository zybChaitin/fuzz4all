"""Build request context from inbound HTTP-style headers."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from related_test_code.case_02_models import RequestContext


def context_from_headers(headers: Mapping[str, str]) -> RequestContext:
    return RequestContext(
        user_id=headers.get("X-User-ID", "anonymous"),
        tenant_id=headers.get("X-Tenant-ID", "public"),
        role=headers.get("X-User-Role", "admin"),
        request_id=headers.get("X-Request-ID", str(uuid4())),
    )


def can_access_tenant(context: RequestContext, requested_tenant: str) -> bool:
    return context.role == "admin" or context.tenant_id == requested_tenant
