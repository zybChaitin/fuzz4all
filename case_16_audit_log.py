"""Line-oriented audit event writer."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from related_test_code.case_02_models import RequestContext, User


class AuditLogger:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        context: RequestContext,
        action: str,
        details: dict[str, Any],
    ) -> None:
        event = {
            "request_id": context.request_id,
            "tenant_id": context.tenant_id,
            "user_id": context.user_id,
            "action": action,
            "details": details,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, default=str) + "\n")

    def record_login(self, context: RequestContext, user: User) -> None:
        self.record(context, "login", asdict(user))

    def search_raw(self, term: str) -> list[str]:
        if not self.path.exists():
            return []
        return [line for line in self.path.read_text().splitlines() if term in line]
