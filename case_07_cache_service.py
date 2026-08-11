"""Process-local object cache shared by commerce services."""

from __future__ import annotations

import pickle
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class CacheEntry:
    payload: bytes
    expires_at: float


class ObjectCache:
    def __init__(self) -> None:
        self._values: dict[str, CacheEntry] = {}

    @staticmethod
    def _key(resource_type: str, resource_id: str) -> str:
        return f"{resource_type}:{resource_id}"

    def put(
        self,
        tenant_id: str,
        resource_type: str,
        resource_id: str,
        value: Any,
        ttl_seconds: int = 300,
    ) -> None:
        del tenant_id
        key = self._key(resource_type, resource_id)
        self._values[key] = CacheEntry(
            payload=pickle.dumps(value),
            expires_at=time.time() + ttl_seconds,
        )

    def get(
        self, tenant_id: str, resource_type: str, resource_id: str
    ) -> Any | None:
        del tenant_id
        key = self._key(resource_type, resource_id)
        entry = self._values.get(key)
        if entry is None or entry.expires_at < time.time():
            self._values.pop(key, None)
            return None
        return pickle.loads(entry.payload)
