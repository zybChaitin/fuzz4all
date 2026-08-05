#!/usr/bin/env python3
"""Single-file RVAgent regrouping benchmark tool for any Git repository.

This tool creates a fixed-diff benchmark PR, previews regrouping plans, prints worker environment presets, retriggers the same PR, and validates review_reorganization_plan.json artifacts.

The create command creates two branches in the current repository (or --repo path):

1. A benchmark base branch containing a clean, self-contained Python fixture.
2. A benchmark PR branch that modifies the fixture across multiple changed files
   and introduces a known set of review-worthy regressions while keeping the
   code syntactically valid and the included happy-path tests passing.

Use this only in a fork, private mirror, or disposable test repository. Do not
open a synthetic benchmark PR against an upstream open-source project.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import random
import shlex
import subprocess
import sys
import textwrap
import unittest
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, Mapping

DEFAULT_FIXTURE_ROOT = "rvagent_regroup_fixture"
DEFAULT_BASE_BRANCH = "benchmark/rvagent-regroup-base"
DEFAULT_PR_BRANCH = "benchmark/rvagent-regroup-medium"
DEFAULT_COMMIT_AUTHOR_NAME = "RVAgent Benchmark"
DEFAULT_COMMIT_AUTHOR_EMAIL = "rvagent-benchmark@example.invalid"


def _d(value: str) -> str:
    return textwrap.dedent(value).lstrip("\n").rstrip() + "\n"


BASE_FILES: Dict[str, str] = {
    "src/common/config.py": _d(
        r'''
        """Runtime configuration helpers used by the benchmark fixture."""

        from __future__ import annotations

        import os
        from dataclasses import dataclass


        TRUE_VALUES = {"1", "true", "yes", "on"}
        FALSE_VALUES = {"0", "false", "no", "off"}


        def read_bool(name: str, default: bool = False) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            normalized = raw.strip().lower()
            if normalized in TRUE_VALUES:
                return True
            if normalized in FALSE_VALUES:
                return False
            raise ValueError(f"{name} must be a boolean value, got {raw!r}")


        def read_positive_int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            value = int(raw)
            if value <= 0:
                raise ValueError(f"{name} must be positive, got {value}")
            return value


        @dataclass(frozen=True)
        class ServiceLimits:
            retries: int
            timeout_seconds: int
            enable_cache: bool


        def load_limits() -> ServiceLimits:
            return ServiceLimits(
                retries=read_positive_int("FIXTURE_RETRIES", 3),
                timeout_seconds=read_positive_int("FIXTURE_TIMEOUT", 10),
                enable_cache=read_bool("FIXTURE_ENABLE_CACHE", True),
            )
        '''
    ),
    "src/common/cache.py": _d(
        r'''
        """Small thread-safe TTL cache for benchmark fixture services."""

        from __future__ import annotations

        import threading
        import time
        from dataclasses import dataclass
        from typing import Callable, Dict, Generic, Optional, TypeVar


        T = TypeVar("T")


        @dataclass
        class CacheEntry(Generic[T]):
            value: T
            expires_at: float


        class TTLCache(Generic[T]):
            def __init__(self, ttl_seconds: float) -> None:
                if ttl_seconds <= 0:
                    raise ValueError("ttl_seconds must be positive")
                self._ttl_seconds = ttl_seconds
                self._items: Dict[str, CacheEntry[T]] = {}
                self._lock = threading.RLock()

            def _now(self) -> float:
                return time.monotonic()

            def get(self, key: str) -> Optional[T]:
                with self._lock:
                    entry = self._items.get(key)
                    if entry is None:
                        return None
                    if entry.expires_at <= self._now():
                        self._items.pop(key, None)
                        return None
                    return entry.value

            def put(self, key: str, value: T) -> None:
                with self._lock:
                    self._items[key] = CacheEntry(
                        value=value,
                        expires_at=self._now() + self._ttl_seconds,
                    )

            def get_or_load(self, key: str, loader: Callable[[], T]) -> T:
                with self._lock:
                    cached = self.get(key)
                    if cached is not None:
                        return cached
                    value = loader()
                    self.put(key, value)
                    return value

            def invalidate_prefix(self, prefix: str) -> int:
                with self._lock:
                    keys = [key for key in self._items if key.startswith(prefix)]
                    for key in keys:
                        self._items.pop(key, None)
                    return len(keys)

            def size(self) -> int:
                with self._lock:
                    return len(self._items)
        '''
    ),
    "src/auth/token_store.py": _d(
        r'''
        """One-time login token storage."""

        from __future__ import annotations

        import secrets
        import threading
        import time
        from dataclasses import dataclass
        from typing import Dict, Optional


        @dataclass(frozen=True)
        class TokenRecord:
            user_id: str
            expires_at: float


        class OneTimeTokenStore:
            def __init__(self) -> None:
                self._records: Dict[str, TokenRecord] = {}
                self._lock = threading.Lock()

            def issue(self, user_id: str, ttl_seconds: float = 300.0) -> str:
                if ttl_seconds <= 0:
                    raise ValueError("ttl_seconds must be positive")
                token = secrets.token_urlsafe(24)
                record = TokenRecord(
                    user_id=user_id,
                    expires_at=time.monotonic() + ttl_seconds,
                )
                with self._lock:
                    self._records[token] = record
                return token

            def consume(self, token: str) -> Optional[str]:
                with self._lock:
                    record = self._records.pop(token, None)
                if record is None:
                    return None
                if record.expires_at <= time.monotonic():
                    return None
                return record.user_id

            def revoke_user(self, user_id: str) -> int:
                with self._lock:
                    tokens = [
                        token
                        for token, record in self._records.items()
                        if record.user_id == user_id
                    ]
                    for token in tokens:
                        self._records.pop(token, None)
                return len(tokens)
        '''
    ),
    "src/auth/service.py": _d(
        r'''
        """Authentication service used by the regrouping benchmark."""

        from __future__ import annotations

        from dataclasses import dataclass
        from typing import Dict, Optional

        from rvagent_regroup_fixture.src.auth.token_store import OneTimeTokenStore


        @dataclass(frozen=True)
        class User:
            user_id: str
            email: str
            active: bool = True


        class AuthService:
            def __init__(self, token_store: OneTimeTokenStore) -> None:
                self._token_store = token_store
                self._users_by_email: Dict[str, User] = {}
                self._users_by_id: Dict[str, User] = {}

            @staticmethod
            def _normalize_email(email: str) -> str:
                return email.strip().casefold()

            def register(self, user: User) -> None:
                normalized = self._normalize_email(user.email)
                if normalized in self._users_by_email:
                    raise ValueError("email already registered")
                self._users_by_email[normalized] = user
                self._users_by_id[user.user_id] = user

            def request_login(self, email: str) -> str:
                user = self._users_by_email.get(self._normalize_email(email))
                if user is None or not user.active:
                    raise LookupError("active user not found")
                return self._token_store.issue(user.user_id)

            def authenticate(self, email: str, token: str) -> Optional[User]:
                user = self._users_by_email.get(self._normalize_email(email))
                if user is None or not user.active:
                    return None
                token_user_id = self._token_store.consume(token)
                if token_user_id != user.user_id:
                    return None
                return self._users_by_id.get(token_user_id)
        '''
    ),
    "src/checkout/pricing.py": _d(
        r'''
        """Deterministic monetary calculations for checkout."""

        from __future__ import annotations

        from dataclasses import dataclass
        from decimal import Decimal, ROUND_HALF_UP
        from typing import Iterable


        MONEY = Decimal("0.01")


        @dataclass(frozen=True)
        class LineItem:
            unit_price: Decimal
            quantity: int

            def subtotal(self) -> Decimal:
                if self.quantity <= 0:
                    raise ValueError("quantity must be positive")
                return self.unit_price * self.quantity


        @dataclass(frozen=True)
        class PriceBreakdown:
            subtotal: Decimal
            discount: Decimal
            tax: Decimal
            total: Decimal


        def _money(value: Decimal) -> Decimal:
            return value.quantize(MONEY, rounding=ROUND_HALF_UP)


        def calculate_total(
            items: Iterable[LineItem],
            discount_rate: Decimal,
            tax_rate: Decimal,
        ) -> PriceBreakdown:
            if not Decimal("0") <= discount_rate <= Decimal("1"):
                raise ValueError("discount_rate must be between 0 and 1")
            if tax_rate < Decimal("0"):
                raise ValueError("tax_rate must be non-negative")

            subtotal = _money(sum((item.subtotal() for item in items), Decimal("0")))
            discount = _money(subtotal * discount_rate)
            taxable = subtotal - discount
            tax = _money(taxable * tax_rate)
            total = _money(taxable + tax)
            return PriceBreakdown(
                subtotal=subtotal,
                discount=discount,
                tax=tax,
                total=total,
            )
        '''
    ),
    "src/checkout/service.py": _d(
        r'''
        """Checkout orchestration with idempotency protection."""

        from __future__ import annotations

        from dataclasses import dataclass
        from decimal import Decimal
        from typing import Callable, Dict, Iterable

        from rvagent_regroup_fixture.src.checkout.pricing import (
            LineItem,
            PriceBreakdown,
            calculate_total,
        )


        @dataclass(frozen=True)
        class CheckoutResult:
            order_id: str
            payment_id: str
            pricing: PriceBreakdown


        PaymentGateway = Callable[[str, Decimal], str]


        class CheckoutService:
            def __init__(self, payment_gateway: PaymentGateway) -> None:
                self._payment_gateway = payment_gateway
                self._completed: Dict[str, CheckoutResult] = {}

            def checkout(
                self,
                *,
                idempotency_key: str,
                order_id: str,
                items: Iterable[LineItem],
                discount_rate: Decimal,
                tax_rate: Decimal,
            ) -> CheckoutResult:
                if not idempotency_key:
                    raise ValueError("idempotency_key is required")
                cached = self._completed.get(idempotency_key)
                if cached is not None:
                    return cached

                pricing = calculate_total(items, discount_rate, tax_rate)
                payment_id = self._payment_gateway(order_id, pricing.total)
                result = CheckoutResult(
                    order_id=order_id,
                    payment_id=payment_id,
                    pricing=pricing,
                )
                self._completed[idempotency_key] = result
                return result
        '''
    ),
    "src/notifications/dedupe.py": _d(
        r'''
        """Notification delivery deduplication state."""

        from __future__ import annotations

        import threading
        from typing import Set


        class DeliveryDedupe:
            def __init__(self) -> None:
                self._delivered: Set[str] = set()
                self._in_flight: Set[str] = set()
                self._lock = threading.Lock()

            def begin(self, message_id: str) -> bool:
                with self._lock:
                    if message_id in self._delivered or message_id in self._in_flight:
                        return False
                    self._in_flight.add(message_id)
                    return True

            def commit(self, message_id: str) -> None:
                with self._lock:
                    self._in_flight.discard(message_id)
                    self._delivered.add(message_id)

            def rollback(self, message_id: str) -> None:
                with self._lock:
                    self._in_flight.discard(message_id)

            def was_delivered(self, message_id: str) -> bool:
                with self._lock:
                    return message_id in self._delivered
        '''
    ),
    "src/notifications/dispatcher.py": _d(
        r'''
        """Notification dispatch with per-message failure isolation."""

        from __future__ import annotations

        from dataclasses import dataclass
        from typing import Callable, Iterable, List

        from rvagent_regroup_fixture.src.notifications.dedupe import DeliveryDedupe


        @dataclass(frozen=True)
        class Notification:
            message_id: str
            destination: str
            body: str


        @dataclass(frozen=True)
        class DeliveryResult:
            message_id: str
            delivered: bool
            error: str | None = None


        Sender = Callable[[Notification], None]


        class NotificationDispatcher:
            def __init__(self, sender: Sender, dedupe: DeliveryDedupe) -> None:
                self._sender = sender
                self._dedupe = dedupe

            def dispatch_one(self, notification: Notification) -> DeliveryResult:
                if not self._dedupe.begin(notification.message_id):
                    return DeliveryResult(notification.message_id, delivered=True)
                try:
                    self._sender(notification)
                except Exception as exc:
                    self._dedupe.rollback(notification.message_id)
                    return DeliveryResult(
                        notification.message_id,
                        delivered=False,
                        error=str(exc),
                    )
                self._dedupe.commit(notification.message_id)
                return DeliveryResult(notification.message_id, delivered=True)

            def dispatch_batch(
                self, notifications: Iterable[Notification]
            ) -> List[DeliveryResult]:
                return [self.dispatch_one(notification) for notification in notifications]
        '''
    ),
    "tests/common/test_cache.py": _d(
        r'''
        from __future__ import annotations

        import os
        import unittest

        from rvagent_regroup_fixture.src.common.cache import TTLCache
        from rvagent_regroup_fixture.src.common.config import read_bool


        class CacheTests(unittest.TestCase):
            def test_loader_runs_once_for_cached_key(self) -> None:
                cache: TTLCache[str] = TTLCache(10)
                calls = 0

                def loader() -> str:
                    nonlocal calls
                    calls += 1
                    return "value"

                self.assertEqual(cache.get_or_load("k", loader), "value")
                self.assertEqual(cache.get_or_load("k", loader), "value")
                self.assertEqual(calls, 1)

            def test_false_environment_value_is_false(self) -> None:
                previous = os.environ.get("FIXTURE_FLAG")
                os.environ["FIXTURE_FLAG"] = "false"
                try:
                    self.assertFalse(read_bool("FIXTURE_FLAG", True))
                finally:
                    if previous is None:
                        os.environ.pop("FIXTURE_FLAG", None)
                    else:
                        os.environ["FIXTURE_FLAG"] = previous

            def test_invalidate_prefix_removes_matching_keys(self) -> None:
                cache: TTLCache[str] = TTLCache(10)
                cache.put("user:1", "a")
                cache.put("user:2", "b")
                cache.put("order:1", "c")
                self.assertEqual(cache.invalidate_prefix("user:"), 2)
                self.assertEqual(cache.size(), 1)
        '''
    ),
    "tests/auth/test_service.py": _d(
        r'''
        from __future__ import annotations

        import unittest

        from rvagent_regroup_fixture.src.auth.service import AuthService, User
        from rvagent_regroup_fixture.src.auth.token_store import OneTimeTokenStore


        class AuthServiceTests(unittest.TestCase):
            def setUp(self) -> None:
                self.store = OneTimeTokenStore()
                self.service = AuthService(self.store)
                self.user = User("user-1", "Alice@example.com")
                self.service.register(self.user)

            def test_email_matching_is_case_insensitive(self) -> None:
                token = self.service.request_login("alice@EXAMPLE.com")
                authenticated = self.service.authenticate("ALICE@example.com", token)
                self.assertEqual(authenticated, self.user)

            def test_token_is_one_time_use(self) -> None:
                token = self.service.request_login("alice@example.com")
                self.assertEqual(
                    self.service.authenticate("alice@example.com", token), self.user
                )
                self.assertIsNone(
                    self.service.authenticate("alice@example.com", token)
                )
        '''
    ),
    "tests/checkout/test_checkout.py": _d(
        r'''
        from __future__ import annotations

        import unittest
        from decimal import Decimal

        from rvagent_regroup_fixture.src.checkout.pricing import LineItem
        from rvagent_regroup_fixture.src.checkout.service import CheckoutService


        class CheckoutTests(unittest.TestCase):
            def test_discount_is_applied_before_tax(self) -> None:
                calls: list[tuple[str, Decimal]] = []

                def gateway(order_id: str, total: Decimal) -> str:
                    calls.append((order_id, total))
                    return "payment-1"

                service = CheckoutService(gateway)
                result = service.checkout(
                    idempotency_key="key-1",
                    order_id="order-1",
                    items=[LineItem(Decimal("100.00"), 1)],
                    discount_rate=Decimal("0.10"),
                    tax_rate=Decimal("0.20"),
                )
                self.assertEqual(result.pricing.total, Decimal("108.00"))
                self.assertEqual(calls, [("order-1", Decimal("108.00"))])

            def test_successful_request_is_idempotent(self) -> None:
                calls = 0

                def gateway(_order_id: str, _total: Decimal) -> str:
                    nonlocal calls
                    calls += 1
                    return "payment-1"

                service = CheckoutService(gateway)
                kwargs = {
                    "idempotency_key": "same-key",
                    "order_id": "order-1",
                    "items": [LineItem(Decimal("5.00"), 2)],
                    "discount_rate": Decimal("0"),
                    "tax_rate": Decimal("0"),
                }
                service.checkout(**kwargs)
                service.checkout(**kwargs)
                self.assertEqual(calls, 1)
        '''
    ),
    "tests/notifications/test_dispatcher.py": _d(
        r'''
        from __future__ import annotations

        import unittest

        from rvagent_regroup_fixture.src.notifications.dedupe import DeliveryDedupe
        from rvagent_regroup_fixture.src.notifications.dispatcher import (
            Notification,
            NotificationDispatcher,
        )


        class NotificationTests(unittest.TestCase):
            def test_failed_delivery_can_be_retried(self) -> None:
                attempts = 0

                def sender(_notification: Notification) -> None:
                    nonlocal attempts
                    attempts += 1
                    if attempts == 1:
                        raise RuntimeError("temporary failure")

                dispatcher = NotificationDispatcher(sender, DeliveryDedupe())
                notification = Notification("message-1", "user@example.com", "hello")
                first = dispatcher.dispatch_one(notification)
                second = dispatcher.dispatch_one(notification)
                self.assertFalse(first.delivered)
                self.assertTrue(second.delivered)
                self.assertEqual(attempts, 2)

            def test_batch_continues_after_one_failure(self) -> None:
                def sender(notification: Notification) -> None:
                    if notification.message_id == "bad":
                        raise RuntimeError("boom")

                dispatcher = NotificationDispatcher(sender, DeliveryDedupe())
                results = dispatcher.dispatch_batch(
                    [
                        Notification("bad", "a@example.com", "a"),
                        Notification("good", "b@example.com", "b"),
                    ]
                )
                self.assertEqual([result.delivered for result in results], [False, True])
        '''
    ),
    "src/inventory/models.py": _d(
        r'''
        """Inventory data models."""

        from __future__ import annotations

        from dataclasses import dataclass


        @dataclass
        class StockItem:
            sku: str
            available: int
            reserved: int = 0

            def reserve(self, quantity: int) -> None:
                if quantity <= 0:
                    raise ValueError("quantity must be positive")
                if quantity > self.available:
                    raise ValueError("insufficient stock")
                self.available -= quantity
                self.reserved += quantity

            def release(self, quantity: int) -> None:
                if quantity <= 0 or quantity > self.reserved:
                    raise ValueError("invalid release quantity")
                self.reserved -= quantity
                self.available += quantity
        '''
    ),
    "src/inventory/reservations.py": _d(
        r'''
        """Thread-safe in-memory reservation store."""

        from __future__ import annotations

        import threading
        from typing import Dict

        from rvagent_regroup_fixture.src.inventory.models import StockItem


        class ReservationStore:
            def __init__(self) -> None:
                self._items: Dict[str, StockItem] = {}
                self._lock = threading.Lock()

            def add(self, item: StockItem) -> None:
                with self._lock:
                    self._items[item.sku] = item

            def reserve(self, sku: str, quantity: int) -> None:
                with self._lock:
                    item = self._items.get(sku)
                    if item is None:
                        raise LookupError(sku)
                    item.reserve(quantity)

            def release(self, sku: str, quantity: int) -> None:
                with self._lock:
                    item = self._items.get(sku)
                    if item is None:
                        raise LookupError(sku)
                    item.release(quantity)

            def available(self, sku: str) -> int:
                with self._lock:
                    item = self._items.get(sku)
                    if item is None:
                        raise LookupError(sku)
                    return item.available
        '''
    ),
    "src/inventory/service.py": _d(
        r'''
        """Inventory reservation orchestration."""

        from __future__ import annotations

        from dataclasses import dataclass
        from typing import Dict

        from rvagent_regroup_fixture.src.inventory.reservations import ReservationStore


        @dataclass(frozen=True)
        class Reservation:
            order_id: str
            sku: str
            quantity: int


        class InventoryService:
            def __init__(self, store: ReservationStore) -> None:
                self._store = store
                self._reservations: Dict[str, Reservation] = {}

            def reserve(self, order_id: str, sku: str, quantity: int) -> Reservation:
                existing = self._reservations.get(order_id)
                if existing is not None:
                    return existing
                self._store.reserve(sku, quantity)
                reservation = Reservation(order_id, sku, quantity)
                self._reservations[order_id] = reservation
                return reservation

            def cancel(self, order_id: str) -> bool:
                reservation = self._reservations.pop(order_id, None)
                if reservation is None:
                    return False
                self._store.release(reservation.sku, reservation.quantity)
                return True
        '''
    ),
    "tests/inventory/test_service.py": _d(
        r'''
        from __future__ import annotations

        import unittest

        from rvagent_regroup_fixture.src.inventory.models import StockItem
        from rvagent_regroup_fixture.src.inventory.reservations import ReservationStore
        from rvagent_regroup_fixture.src.inventory.service import InventoryService


        class InventoryTests(unittest.TestCase):
            def test_reserve_and_cancel(self) -> None:
                store = ReservationStore()
                store.add(StockItem("sku-1", available=10))
                service = InventoryService(store)
                reservation = service.reserve("order-1", "sku-1", 3)
                self.assertEqual(reservation.quantity, 3)
                self.assertEqual(store.available("sku-1"), 7)
                self.assertTrue(service.cancel("order-1"))
                self.assertEqual(store.available("sku-1"), 10)
        '''
    ),
}


MR_FILES: Dict[str, str] = {
    "src/common/config.py": _d(
        r'''
        """Runtime configuration helpers used by the benchmark fixture."""

        from __future__ import annotations

        import os
        from dataclasses import dataclass


        def read_bool(name: str, default: bool = False) -> bool:
            raw = os.getenv(name)
            if raw is None:
                return default
            return bool(raw.strip())


        def read_positive_int(name: str, default: int) -> int:
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            return int(raw)


        @dataclass(frozen=True)
        class ServiceLimits:
            retries: int
            timeout_seconds: int
            enable_cache: bool


        def load_limits() -> ServiceLimits:
            return ServiceLimits(
                retries=read_positive_int("FIXTURE_RETRIES", 3),
                timeout_seconds=read_positive_int("FIXTURE_TIMEOUT", 10),
                enable_cache=read_bool("FIXTURE_ENABLE_CACHE", True),
            )
        '''
    ),
    "src/common/cache.py": _d(
        r'''
        """Small TTL cache for benchmark fixture services."""

        from __future__ import annotations

        import time
        from dataclasses import dataclass
        from typing import Callable, Dict, Generic, Optional, TypeVar


        T = TypeVar("T")


        @dataclass
        class CacheEntry(Generic[T]):
            value: T
            expires_at: float


        class TTLCache(Generic[T]):
            def __init__(self, ttl_seconds: float) -> None:
                self._ttl_seconds = ttl_seconds
                self._items: Dict[str, CacheEntry[T]] = {}

            def get(self, key: str) -> Optional[T]:
                entry = self._items.get(key)
                if entry is None:
                    return None
                if entry.expires_at <= time.time():
                    self._items.pop(key, None)
                    return None
                return entry.value

            def put(self, key: str, value: T) -> None:
                self._items[key] = CacheEntry(
                    value=value,
                    expires_at=time.time() + self._ttl_seconds,
                )

            def get_or_load(self, key: str, loader: Callable[[], T]) -> T:
                cached = self.get(key)
                if cached is not None:
                    return cached
                value = loader()
                self.put(key, value)
                return value

            def invalidate_prefix(self, prefix: str) -> int:
                removed = 0
                for key in self._items:
                    if key.startswith(prefix):
                        del self._items[key]
                        removed += 1
                return removed

            def size(self) -> int:
                return len(self._items)
        '''
    ),
    "src/auth/token_store.py": _d(
        r'''
        """One-time login token storage."""

        from __future__ import annotations

        import secrets
        import time
        from dataclasses import dataclass
        from typing import Dict, Optional


        @dataclass(frozen=True)
        class TokenRecord:
            user_id: str
            expires_at: float


        class OneTimeTokenStore:
            def __init__(self) -> None:
                self._records: Dict[str, TokenRecord] = {}

            def issue(self, user_id: str, ttl_seconds: float = 300.0) -> str:
                token = secrets.token_urlsafe(24)
                self._records[token] = TokenRecord(
                    user_id=user_id,
                    expires_at=time.time() + ttl_seconds,
                )
                return token

            def consume(self, token: str) -> Optional[str]:
                record = self._records.get(token)
                if record is None:
                    return None
                if record.expires_at <= time.time():
                    self._records.pop(token, None)
                    return None
                del self._records[token]
                return record.user_id

            def revoke_user(self, user_id: str) -> int:
                removed = 0
                for token, record in list(self._records.items()):
                    if record.user_id == user_id:
                        self._records.pop(token, None)
                        removed += 1
                return removed
        '''
    ),
    "src/auth/service.py": _d(
        r'''
        """Authentication service used by the regrouping benchmark."""

        from __future__ import annotations

        from dataclasses import dataclass
        from typing import Dict, Optional

        from rvagent_regroup_fixture.src.auth.token_store import OneTimeTokenStore


        @dataclass(frozen=True)
        class User:
            user_id: str
            email: str
            active: bool = True


        class AuthService:
            def __init__(self, token_store: OneTimeTokenStore) -> None:
                self._token_store = token_store
                self._users_by_email: Dict[str, User] = {}
                self._users_by_id: Dict[str, User] = {}

            @staticmethod
            def _normalize_email(email: str) -> str:
                return email.strip().lower()

            def register(self, user: User) -> None:
                normalized = self._normalize_email(user.email)
                self._users_by_email[normalized] = user
                self._users_by_id[user.user_id] = user

            def request_login(self, email: str) -> str:
                user = self._users_by_email.get(self._normalize_email(email))
                if user is None:
                    raise LookupError("user not found")
                return self._token_store.issue(user.user_id)

            def authenticate(self, email: str, token: str) -> Optional[User]:
                token_user_id = self._token_store.consume(token)
                user = self._users_by_email.get(email.strip())
                if user is None or token_user_id != user.user_id:
                    return None
                return self._users_by_id.get(token_user_id)
        '''
    ),
    "src/checkout/pricing.py": _d(
        r'''
        """Checkout price calculations."""

        from __future__ import annotations

        from dataclasses import dataclass
        from decimal import Decimal
        from typing import Iterable


        @dataclass(frozen=True)
        class LineItem:
            unit_price: Decimal
            quantity: int

            def subtotal(self) -> Decimal:
                return self.unit_price * self.quantity


        @dataclass(frozen=True)
        class PriceBreakdown:
            subtotal: Decimal
            discount: Decimal
            tax: Decimal
            total: Decimal


        def calculate_total(
            items: Iterable[LineItem],
            discount_rate: Decimal,
            tax_rate: Decimal,
        ) -> PriceBreakdown:
            subtotal = sum((item.subtotal() for item in items), Decimal("0"))
            tax = subtotal * tax_rate
            discount = subtotal * discount_rate
            total = round(float(subtotal + tax - discount), 2)
            return PriceBreakdown(
                subtotal=subtotal,
                discount=discount,
                tax=tax,
                total=Decimal(str(total)),
            )
        '''
    ),
    "src/checkout/service.py": _d(
        r'''
        """Checkout orchestration with idempotency protection."""

        from __future__ import annotations

        from dataclasses import dataclass
        from decimal import Decimal
        from typing import Callable, Dict, Iterable

        from rvagent_regroup_fixture.src.checkout.pricing import (
            LineItem,
            PriceBreakdown,
            calculate_total,
        )


        @dataclass(frozen=True)
        class CheckoutResult:
            order_id: str
            payment_id: str
            pricing: PriceBreakdown


        PaymentGateway = Callable[[str, Decimal], str]


        class CheckoutService:
            def __init__(self, payment_gateway: PaymentGateway) -> None:
                self._payment_gateway = payment_gateway
                self._completed: Dict[str, CheckoutResult] = {}

            def checkout(
                self,
                *,
                idempotency_key: str,
                order_id: str,
                items: Iterable[LineItem],
                discount_rate: Decimal,
                tax_rate: Decimal,
            ) -> CheckoutResult:
                cached = self._completed.get(idempotency_key)
                if cached is not None:
                    return cached

                pricing = calculate_total(items, discount_rate, tax_rate)
                pending = CheckoutResult(
                    order_id=order_id,
                    payment_id="pending",
                    pricing=pricing,
                )
                self._completed[idempotency_key] = pending
                payment_id = self._payment_gateway(order_id, pricing.total)
                result = CheckoutResult(order_id, payment_id, pricing)
                self._completed[idempotency_key] = result
                return result
        '''
    ),
    "src/notifications/dedupe.py": _d(
        r'''
        """Notification delivery deduplication state."""

        from __future__ import annotations

        import threading
        from typing import Set


        class DeliveryDedupe:
            def __init__(self) -> None:
                self._delivered: Set[str] = set()
                self._lock = threading.Lock()

            def begin(self, message_id: str) -> bool:
                with self._lock:
                    if message_id in self._delivered:
                        return False
                    self._delivered.add(message_id)
                    return True

            def commit(self, message_id: str) -> None:
                return None

            def rollback(self, message_id: str) -> None:
                return None

            def was_delivered(self, message_id: str) -> bool:
                with self._lock:
                    return message_id in self._delivered
        '''
    ),
    "src/notifications/dispatcher.py": _d(
        r'''
        """Notification dispatch for the benchmark fixture."""

        from __future__ import annotations

        from dataclasses import dataclass
        from typing import Callable, Iterable, List

        from rvagent_regroup_fixture.src.notifications.dedupe import DeliveryDedupe


        @dataclass(frozen=True)
        class Notification:
            message_id: str
            destination: str
            body: str


        @dataclass(frozen=True)
        class DeliveryResult:
            message_id: str
            delivered: bool
            error: str | None = None


        Sender = Callable[[Notification], None]


        class NotificationDispatcher:
            def __init__(self, sender: Sender, dedupe: DeliveryDedupe) -> None:
                self._sender = sender
                self._dedupe = dedupe

            def dispatch_one(self, notification: Notification) -> DeliveryResult:
                if not self._dedupe.begin(notification.message_id):
                    return DeliveryResult(notification.message_id, delivered=True)
                try:
                    self._sender(notification)
                except Exception as exc:
                    return DeliveryResult(
                        notification.message_id,
                        delivered=False,
                        error=str(exc),
                    )
                return DeliveryResult(notification.message_id, delivered=True)

            def dispatch_batch(
                self, notifications: Iterable[Notification]
            ) -> List[DeliveryResult]:
                results: List[DeliveryResult] = []
                for notification in notifications:
                    result = self.dispatch_one(notification)
                    results.append(result)
                    if not result.delivered:
                        break
                return results
        '''
    ),
    "tests/common/test_cache.py": _d(
        r'''
        from __future__ import annotations

        import unittest

        from rvagent_regroup_fixture.src.common.cache import TTLCache


        class CacheTests(unittest.TestCase):
            def test_loader_returns_cached_value(self) -> None:
                cache: TTLCache[str] = TTLCache(10)
                self.assertEqual(cache.get_or_load("k", lambda: "value"), "value")
                self.assertEqual(cache.get("k"), "value")

            def test_size_counts_inserted_items(self) -> None:
                cache: TTLCache[str] = TTLCache(10)
                cache.put("user:1", "a")
                cache.put("order:1", "b")
                self.assertEqual(cache.size(), 2)
        '''
    ),
    "tests/auth/test_service.py": _d(
        r'''
        from __future__ import annotations

        import unittest

        from rvagent_regroup_fixture.src.auth.service import AuthService, User
        from rvagent_regroup_fixture.src.auth.token_store import OneTimeTokenStore


        class AuthServiceTests(unittest.TestCase):
            def test_exact_email_can_authenticate(self) -> None:
                store = OneTimeTokenStore()
                service = AuthService(store)
                user = User("user-1", "alice@example.com")
                service.register(user)
                token = service.request_login("alice@example.com")
                self.assertEqual(
                    service.authenticate("alice@example.com", token), user
                )
        '''
    ),
    "tests/checkout/test_checkout.py": _d(
        r'''
        from __future__ import annotations

        import unittest
        from decimal import Decimal

        from rvagent_regroup_fixture.src.checkout.pricing import LineItem
        from rvagent_regroup_fixture.src.checkout.service import CheckoutService


        class CheckoutTests(unittest.TestCase):
            def test_checkout_calls_gateway(self) -> None:
                calls: list[tuple[str, Decimal]] = []

                def gateway(order_id: str, total: Decimal) -> str:
                    calls.append((order_id, total))
                    return "payment-1"

                service = CheckoutService(gateway)
                result = service.checkout(
                    idempotency_key="key-1",
                    order_id="order-1",
                    items=[LineItem(Decimal("10.00"), 1)],
                    discount_rate=Decimal("0"),
                    tax_rate=Decimal("0"),
                )
                self.assertEqual(result.payment_id, "payment-1")
                self.assertEqual(len(calls), 1)
        '''
    ),
    "tests/notifications/test_dispatcher.py": _d(
        r'''
        from __future__ import annotations

        import unittest

        from rvagent_regroup_fixture.src.notifications.dedupe import DeliveryDedupe
        from rvagent_regroup_fixture.src.notifications.dispatcher import (
            Notification,
            NotificationDispatcher,
        )


        class NotificationTests(unittest.TestCase):
            def test_successful_delivery(self) -> None:
                delivered: list[str] = []

                def sender(notification: Notification) -> None:
                    delivered.append(notification.message_id)

                dispatcher = NotificationDispatcher(sender, DeliveryDedupe())
                result = dispatcher.dispatch_one(
                    Notification("message-1", "user@example.com", "hello")
                )
                self.assertTrue(result.delivered)
                self.assertEqual(delivered, ["message-1"])
        '''
    ),
    "src/inventory/models.py": _d(
        r'''
        """Inventory data models."""

        from __future__ import annotations

        from dataclasses import dataclass


        @dataclass
        class StockItem:
            sku: str
            available: int
            reserved: int = 0

            def reserve(self, quantity: int) -> None:
                if quantity > self.available:
                    raise ValueError("insufficient stock")
                self.available -= quantity
                self.reserved += quantity

            def release(self, quantity: int) -> None:
                self.reserved -= quantity
                self.available += quantity
        '''
    ),
    "src/inventory/reservations.py": _d(
        r'''
        """In-memory reservation store."""

        from __future__ import annotations

        from typing import Dict

        from rvagent_regroup_fixture.src.inventory.models import StockItem


        class ReservationStore:
            def __init__(self) -> None:
                self._items: Dict[str, StockItem] = {}

            def add(self, item: StockItem) -> None:
                self._items[item.sku] = item

            def reserve(self, sku: str, quantity: int) -> None:
                item = self._items[sku]
                if item.available < quantity:
                    raise ValueError("insufficient stock")
                item.reserve(quantity)

            def release(self, sku: str, quantity: int) -> None:
                self._items[sku].release(quantity)

            def available(self, sku: str) -> int:
                return self._items[sku].available
        '''
    ),
    "src/inventory/service.py": _d(
        r'''
        """Inventory reservation orchestration."""

        from __future__ import annotations

        from dataclasses import dataclass
        from typing import Dict

        from rvagent_regroup_fixture.src.inventory.reservations import ReservationStore


        @dataclass(frozen=True)
        class Reservation:
            order_id: str
            sku: str
            quantity: int


        class InventoryService:
            def __init__(self, store: ReservationStore) -> None:
                self._store = store
                self._reservations: Dict[str, Reservation] = {}

            def reserve(self, order_id: str, sku: str, quantity: int) -> Reservation:
                existing = self._reservations.get(order_id)
                if existing is not None:
                    return existing
                reservation = Reservation(order_id, sku, quantity)
                self._reservations[order_id] = reservation
                self._store.reserve(sku, quantity)
                return reservation

            def cancel(self, order_id: str) -> bool:
                reservation = self._reservations.pop(order_id, None)
                if reservation is None:
                    return False
                self._store.release(reservation.sku, reservation.quantity)
                return True
        '''
    ),
    "tests/inventory/test_service.py": _d(
        r'''
        from __future__ import annotations

        import unittest

        from rvagent_regroup_fixture.src.inventory.models import StockItem
        from rvagent_regroup_fixture.src.inventory.reservations import ReservationStore
        from rvagent_regroup_fixture.src.inventory.service import InventoryService


        class InventoryTests(unittest.TestCase):
            def test_reserve_reduces_available_stock(self) -> None:
                store = ReservationStore()
                store.add(StockItem("sku-1", available=10))
                service = InventoryService(store)
                service.reserve("order-1", "sku-1", 3)
                self.assertEqual(store.available("sku-1"), 7)
        '''
    ),
}


PROFILE_PATHS = {
    "small": [
        "src/common/config.py",
        "src/common/cache.py",
    ],
    "medium": [
        "src/common/config.py",
        "src/common/cache.py",
        "src/auth/token_store.py",
        "src/auth/service.py",
        "src/checkout/pricing.py",
        "src/checkout/service.py",
        "src/notifications/dedupe.py",
        "src/notifications/dispatcher.py",
        "tests/common/test_cache.py",
        "tests/auth/test_service.py",
        "tests/checkout/test_checkout.py",
        "tests/notifications/test_dispatcher.py",
    ],
    "large": list(BASE_FILES.keys()),
}


def run(
    command: Iterable[str],
    *,
    cwd: Path,
    check: bool = True,
    capture: bool = False,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    print("+", " ".join(shlex.quote(part) for part in command))
    return subprocess.run(
        list(command),
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture,
        env=merged_env,
    )


def git(repo: Path, *args: str, capture: bool = False) -> str:
    result = run(["git", *args], cwd=repo, capture=capture)
    return result.stdout.strip() if capture else ""


def ensure_git_repo(repo: Path) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=repo,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit(f"Not a Git repository: {repo}")


def ensure_clean(repo: Path) -> None:
    status = git(repo, "status", "--porcelain", capture=True)
    if status:
        raise SystemExit(
            "Working tree is not clean. Commit/stash existing changes first:\n" + status
        )


def branch_exists(repo: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=repo,
    )
    return result.returncode == 0


def configure_identity(repo: Path) -> None:
    name = subprocess.run(
        ["git", "config", "user.name"], cwd=repo, text=True, capture_output=True
    ).stdout.strip()
    email = subprocess.run(
        ["git", "config", "user.email"], cwd=repo, text=True, capture_output=True
    ).stdout.strip()
    if not name:
        git(repo, "config", "user.name", DEFAULT_COMMIT_AUTHOR_NAME)
    if not email:
        git(repo, "config", "user.email", DEFAULT_COMMIT_AUTHOR_EMAIL)


def write_profile(
    repo: Path,
    fixture_root: str,
    files: Mapping[str, str],
    profile_paths: Iterable[str],
) -> None:
    root = repo / fixture_root
    for relative in profile_paths:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(files[relative], encoding="utf-8")


def remove_fixture(repo: Path, fixture_root: str) -> None:
    root = repo / fixture_root
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    try:
        root.rmdir()
    except OSError:
        pass


def estimate_plan(repo: Path, base_branch: str, pr_branch: str, fixture_root: str) -> dict:
    changed = git(
        repo,
        "diff",
        "--name-only",
        f"{base_branch}...{pr_branch}",
        "--",
        fixture_root,
        capture=True,
    ).splitlines()
    items = []
    total = 0
    for path in changed:
        numstat = git(
            repo,
            "diff",
            "--numstat",
            f"{base_branch}...{pr_branch}",
            "--",
            path,
            capture=True,
        ).splitlines()
        additions = deletions = 0
        if numstat:
            left, right, _ = numstat[0].split("\t", 2)
            additions = int(left) if left.isdigit() else 0
            deletions = int(right) if right.isdigit() else 0
        patch = git(
            repo,
            "diff",
            "--no-ext-diff",
            "--unified=3",
            f"{base_branch}...{pr_branch}",
            "--",
            path,
            capture=True,
        )
        changed_lines = additions + deletions
        patch_lines = len(patch.splitlines())
        patch_chars = len(patch)
        patch_kib = math.ceil(patch_chars / 1024) if patch else 0
        weight = 120 + max(changed_lines, patch_lines) + patch_kib * 8
        total += weight
        items.append(
            {
                "path": path,
                "additions": additions,
                "deletions": deletions,
                "patch_lines": patch_lines,
                "patch_chars": patch_chars,
                "estimated_weight": weight,
            }
        )
    selected = 0 if not items else 1
    if len(items) > 1 and total >= 600:
        selected = max(1, min(8, len(items), math.ceil(total / 600)))
    return {
        "changed_files": len(items),
        "total_estimated_weight": total,
        "expected_adaptive_shards": selected,
        "parallel_cap": 8,
        "minimum_total_weight": 600,
        "target_weight_per_agent": 600,
        "items": items,
    }


def run_fixture_tests(repo: Path, fixture_root: str) -> None:
    """Load and run the fixture's unittest modules without package markers."""

    test_root = repo / fixture_root / "tests"
    test_files = sorted(test_root.rglob("test_*.py"))
    if not test_files:
        print(f"No fixture tests selected under {test_root}; compile verification only.")
        return

    sys.path.insert(0, str(repo))
    try:
        suite = unittest.TestSuite()
        loader = unittest.TestLoader()
        for index, path in enumerate(test_files):
            module_name = f"_rvagent_fixture_test_{index}_{path.stem}"
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"cannot load test module: {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            suite.addTests(loader.loadTestsFromModule(module))
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        if not result.wasSuccessful():
            raise RuntimeError("fixture tests failed")
    finally:
        try:
            sys.path.remove(str(repo))
        except ValueError:
            pass


def create_pr(args: argparse.Namespace) -> None:
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    ensure_clean(repo)
    configure_identity(repo)

    profile_paths = PROFILE_PATHS[args.profile]
    base_branch = args.base_branch or f"{DEFAULT_BASE_BRANCH}-{args.profile}"
    pr_branch = args.pr_branch or f"benchmark/rvagent-regroup-{args.profile}"

    if branch_exists(repo, base_branch) or branch_exists(repo, pr_branch):
        raise SystemExit(
            "Benchmark branches already exist. Delete or rename them first:\n"
            f"  git branch -D {base_branch} {pr_branch}"
        )

    start_ref = args.start_ref or git(repo, "rev-parse", "--abbrev-ref", "HEAD", capture=True)
    if start_ref == "HEAD":
        start_ref = git(repo, "rev-parse", "HEAD", capture=True)

    print(f"Creating benchmark base branch {base_branch!r} from {start_ref!r}")
    git(repo, "switch", "-c", base_branch, start_ref)
    remove_fixture(repo, args.fixture_root)
    write_profile(repo, args.fixture_root, BASE_FILES, profile_paths)
    git(repo, "add", "--", args.fixture_root)
    git(
        repo,
        "commit",
        "-m",
        f"test: add RVAgent regrouping benchmark baseline ({args.profile})",
    )

    print(f"Creating benchmark PR branch {pr_branch!r}")
    git(repo, "switch", "-c", pr_branch)
    write_profile(repo, args.fixture_root, MR_FILES, profile_paths)
    git(repo, "add", "--", args.fixture_root)
    git(
        repo,
        "commit",
        "-m",
        f"test: introduce RVAgent regrouping benchmark changes ({args.profile})",
    )

    estimate = estimate_plan(repo, base_branch, pr_branch, args.fixture_root)
    report_path = repo / ".git" / f"rvagent-regroup-estimate-{args.profile}.json"
    report_path.write_text(json.dumps(estimate, indent=2), encoding="utf-8")

    print("\nBenchmark diff summary")
    print("======================")
    print(f"Profile:                  {args.profile}")
    print(f"Changed files:            {estimate['changed_files']}")
    print(f"Estimated total weight:   {estimate['total_estimated_weight']}")
    print(f"Expected adaptive shards: {estimate['expected_adaptive_shards']}")
    print(f"Estimate report:          {report_path}")
    print()
    run(
        [
            "git",
            "diff",
            "--stat",
            f"{base_branch}...{pr_branch}",
            "--",
            args.fixture_root,
        ],
        cwd=repo,
    )

    if args.verify:
        print("\nCompiling fixture code...")
        run([sys.executable, "-m", "compileall", "-q", args.fixture_root], cwd=repo)
        print("Running fixture tests with the portable unittest loader...")
        run_fixture_tests(repo, args.fixture_root)

    if args.push:
        git(repo, "push", "-u", args.remote, base_branch)
        git(repo, "push", "-u", args.remote, pr_branch)

    if args.create_pr:
        if not args.push:
            raise SystemExit("--create-pr requires --push")
        title = args.pr_title or f"RVAgent regrouping benchmark ({args.profile})"
        body = args.pr_body or (
            "Synthetic benchmark PR for comparing RVAgent ReviewInput regrouping "
            "algorithms. This PR targets a benchmark base branch in a fork/private "
            "mirror and is not intended for upstream merge."
        )
        run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                pr_branch,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=repo,
        )

    print("\nDone.")
    print(f"Base branch: {base_branch}")
    print(f"PR branch:   {pr_branch}")
    if not args.push:
        print("Push when ready:")
        print(f"  git push -u {args.remote} {base_branch}")
        print(f"  git push -u {args.remote} {pr_branch}")
    if not args.create_pr:
        print("Create a PR in your fork/private mirror with:")
        print(f"  base: {base_branch}")
        print(f"  head: {pr_branch}")


@dataclass(frozen=True)
class DiffItem:
    path: str
    additions: int
    deletions: int
    patch: str
    weight: int


def _git_capture(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _load_diff_items(
    repo: Path,
    base_ref: str,
    head_ref: str,
    pathspec: str | None,
) -> list[DiffItem]:
    command = ["diff", "--name-only", f"{base_ref}...{head_ref}"]
    if pathspec:
        command.extend(["--", pathspec])
    paths = [
        line.strip()
        for line in _git_capture(repo, *command).splitlines()
        if line.strip()
    ]

    items: list[DiffItem] = []
    for path in paths:
        numstat = _git_capture(
            repo,
            "diff",
            "--numstat",
            f"{base_ref}...{head_ref}",
            "--",
            path,
        )
        additions = 0
        deletions = 0
        if numstat:
            left, right, _ = numstat.split("\t", 2)
            additions = int(left) if left.isdigit() else 0
            deletions = int(right) if right.isdigit() else 0

        patch = _git_capture(
            repo,
            "diff",
            "--no-ext-diff",
            "--unified=3",
            f"{base_ref}...{head_ref}",
            "--",
            path,
        )
        changed_lines = additions + deletions
        patch_lines = len(patch.splitlines())
        patch_kib = math.ceil(len(patch) / 1024) if patch else 0
        weight = 120 + max(changed_lines, patch_lines) + patch_kib * 8
        items.append(
            DiffItem(
                path=path,
                additions=additions,
                deletions=deletions,
                patch=patch,
                weight=weight,
            )
        )
    return items


def _adaptive_shard_count(
    items: list[DiffItem],
    parallel_cap: int,
    minimum_total_weight: int,
    target_weight: int,
) -> int:
    if not items:
        return 0
    if len(items) <= 1 or parallel_cap <= 1:
        return 1
    total_weight = sum(item.weight for item in items)
    if total_weight < minimum_total_weight:
        return 1
    return max(
        1,
        min(
            min(max(parallel_cap, 1), 8),
            len(items),
            math.ceil(total_weight / max(target_weight, 1)),
        ),
    )


def _lpt_groups(items: list[DiffItem], count: int) -> list[list[DiffItem]]:
    if not items:
        return []
    if count <= 1:
        return [list(items)]
    groups: list[list[DiffItem]] = [[] for _ in range(count)]
    loads = [0] * count
    for item in sorted(items, key=lambda value: (-value.weight, value.path)):
        target = min(range(count), key=lambda index: (loads[index], index))
        groups[target].append(item)
        loads[target] += item.weight
    return groups


def _stable_seed(paths: Iterable[str], configured: str | None) -> int:
    if configured:
        try:
            return int(configured)
        except ValueError:
            payload = configured.encode("utf-8")
    else:
        payload = "\n".join(paths).encode("utf-8")
    return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")


def _random_groups(
    items: list[DiffItem],
    count: int,
    configured_seed: str | None,
) -> tuple[list[list[DiffItem]], int]:
    seed = _stable_seed((item.path for item in items), configured_seed)
    if not items:
        return [], seed
    if count <= 1:
        return [list(items)], seed
    shuffled = list(items)
    random.Random(seed).shuffle(shuffled)
    groups: list[list[DiffItem]] = [[] for _ in range(count)]
    for index, item in enumerate(shuffled):
        groups[index % count].append(item)
    return groups, seed


def _normalize_path(path: str) -> str:
    return path.strip().replace("\\", "/").removeprefix("./")


def _directory_distance(left: str, right: str) -> int:
    left_parts = PurePosixPath(_normalize_path(left)).parent.parts
    right_parts = PurePosixPath(_normalize_path(right)).parent.parts
    common = 0
    for left_part, right_part in zip(left_parts, right_parts):
        if left_part != right_part:
            break
        common += 1
    return (len(left_parts) - common) + (len(right_parts) - common)


def _directory_groups(
    items: list[DiffItem],
    count: int,
) -> list[list[DiffItem]]:
    if not items:
        return []
    if count <= 1:
        return [list(items)]

    remaining = sorted(items, key=lambda item: (-item.weight, item.path))
    seeds = [remaining.pop(0)]
    while len(seeds) < count and remaining:
        seed = max(
            remaining,
            key=lambda item: (
                min(
                    _directory_distance(item.path, existing.path)
                    for existing in seeds
                ),
                item.weight,
                item.path,
            ),
        )
        remaining.remove(seed)
        seeds.append(seed)

    groups = [[seed] for seed in seeds]
    loads = [seed.weight for seed in seeds]
    target_load = max(1.0, sum(item.weight for item in items) / count)

    for item in sorted(remaining, key=lambda value: (-value.weight, value.path)):
        def assignment_cost(index: int) -> tuple[float, int, int]:
            average_distance = sum(
                _directory_distance(item.path, peer.path)
                for peer in groups[index]
            ) / len(groups[index])
            return (
                average_distance + loads[index] / target_load,
                loads[index],
                index,
            )

        target = min(range(len(groups)), key=assignment_cost)
        groups[target].append(item)
        loads[target] += item.weight
    return groups


def _group_payload(
    method: str,
    groups: list[list[DiffItem]],
    selected_worker_count: int,
) -> dict:
    return {
        "method": method,
        "selected_worker_count": selected_worker_count,
        "shard_count": len(groups),
        "shards": [
            {
                "shard_index": index,
                "estimated_weight": sum(item.weight for item in group),
                "files": [item.path for item in group],
            }
            for index, group in enumerate(groups)
        ],
    }


def preview_command(args: argparse.Namespace) -> None:
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    items = _load_diff_items(repo, args.base, args.head, args.path)
    parallel_cap = min(max(args.parallel, 1), 8)
    shard_count = _adaptive_shard_count(
        items,
        parallel_cap,
        args.minimum_total_weight,
        args.target_weight,
    )

    methods = (
        ["split-by-single-file", "random", "directory-distance", "lpt"]
        if args.method == "all"
        else [args.method]
    )
    plans: list[dict] = []

    for method in methods:
        if method == "split-by-single-file":
            groups = [[item] for item in items]
            payload = _group_payload(
                method,
                groups,
                min(parallel_cap, len(groups)),
            )
        elif method == "random":
            groups, seed = _random_groups(items, shard_count, args.seed)
            payload = _group_payload(method, groups, len(groups))
            payload["random_seed"] = seed
        elif method == "directory-distance":
            groups = _directory_groups(items, shard_count)
            payload = _group_payload(method, groups, len(groups))
        else:
            groups = _lpt_groups(items, shard_count)
            payload = _group_payload(method, groups, len(groups))
        plans.append(payload)

    output = {
        "base": args.base,
        "head": args.head,
        "pathspec": args.path,
        "changed_files": len(items),
        "total_estimated_weight": sum(item.weight for item in items),
        "adaptive_shard_count": shard_count,
        "parallel_cap": parallel_cap,
        "minimum_total_weight": args.minimum_total_weight,
        "target_weight_per_agent": args.target_weight,
        "items": [
            {
                "path": item.path,
                "additions": item.additions,
                "deletions": item.deletions,
                "estimated_weight": item.weight,
            }
            for item in items
        ],
        "plans": plans,
        "model_note": (
            "The model pipeline is not simulated locally. Validate requested_method "
            "and effective_method in review_reorganization_plan.json."
        ),
    }

    rendered = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(rendered + "\n", encoding="utf-8")
        print(f"Wrote preview: {output_path}")
    print(rendered)


def rerun_command(args: argparse.Namespace) -> None:
    repo = Path(args.repo).expanduser().resolve()
    ensure_git_repo(repo)
    ensure_clean(repo)

    if args.branch:
        current = _git_capture(repo, "rev-parse", "--abbrev-ref", "HEAD")
        if current != args.branch:
            git(repo, "switch", args.branch)

    label_parts = ["benchmark: rerun", args.method]
    if args.model:
        label_parts.append(args.model)
    label_parts.append(f"run-{args.run}")
    message = " ".join(label_parts)

    git(repo, "commit", "--allow-empty", "-m", message)
    if args.push:
        git(repo, "push", args.remote, "HEAD")
    else:
        print("Created an empty commit. Push it to retrigger the same PR:")
        print(f"  git push {args.remote} HEAD")


def env_command(args: argparse.Namespace) -> None:
    lines = [
        "# Configure these variables in the actual RVAgent/MonkeyScan worker.",
        "# Printing them here does not modify a remote pressure-test environment.",
        f"export PARALLEL_WORKS={min(max(args.parallel, 1), 8)}",
        f"export PARALLEL_SPLITTING_METHOD={shlex.quote(args.method)}",
        f"export PARALLEL_SPLITTING_SEED={shlex.quote(args.seed)}",
        f"export PARALLEL_MIN_TOTAL_WEIGHT={args.minimum_total_weight}",
        f"export PARALLEL_TARGET_WEIGHT_PER_AGENT={args.target_weight}",
        f"export PARALLEL_MODEL_SPLITTING_TIMEOUT={args.model_timeout}",
    ]

    if args.runtime == "claude-code":
        lines.extend(
            [
                "export AGENT_RUNTIME=claude-code",
                f"export ANTHROPIC_MODEL={shlex.quote(args.model_id or '<CLAUDE_MODEL_ID>')}",
                f"export ANTHROPIC_BASE_URL={shlex.quote(args.base_url or '<MODEL_GATEWAY_URL>')}",
                "export ANTHROPIC_AUTH_TOKEN='<SECRET>'",
            ]
        )
    elif args.runtime == "codex":
        lines.extend(
            [
                "export AGENT_RUNTIME=codex",
                f"export OPENAI_MODEL={shlex.quote(args.model_id or '<CODEX_MODEL_ID>')}",
                f"export OPENAI_BASE_URL={shlex.quote(args.base_url or '<MODEL_GATEWAY_URL>')}",
                "export OPENAI_API_KEY='<SECRET>'",
            ]
        )

    print("\n".join(lines))


def validate_plan_command(args: argparse.Namespace) -> None:
    path = Path(args.plan).expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))

    requested = payload.get("requested_method")
    effective = payload.get("effective_method")
    warnings = payload.get("warnings") or []
    selected_agents = payload.get("selected_agent_count")
    shard_count = payload.get("shard_count")
    shards = payload.get("shards") or []

    summary = {
        "plan": str(path),
        "requested_method": requested,
        "effective_method": effective,
        "selected_agent_count": selected_agents,
        "shard_count": shard_count,
        "warnings": warnings,
        "shards": [
            {
                "shard_index": shard.get("shard_index"),
                "estimated_weight": shard.get("estimated_weight"),
                "file_paths": shard.get("file_paths") or shard.get("files") or [],
            }
            for shard in shards
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    errors: list[str] = []
    if args.expected_method and requested != args.expected_method:
        errors.append(
            f"requested_method={requested!r}, expected {args.expected_method!r}"
        )
    if args.require_effective and effective != args.require_effective:
        errors.append(
            f"effective_method={effective!r}, expected {args.require_effective!r}"
        )
    if args.no_fallback and requested != effective:
        errors.append(
            f"fallback detected: requested={requested!r}, effective={effective!r}"
        )
    if errors:
        raise SystemExit("Plan validation failed:\n- " + "\n- ".join(errors))


def create_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "create",
        help="Create benchmark base/head branches and optionally push/open a PR",
    )
    parser.add_argument("--repo", default=".", help="Target Git repository")
    parser.add_argument(
        "--profile",
        choices=sorted(PROFILE_PATHS),
        default="medium",
        help="small≈1 shard, medium≈4 shards, large≈5-6 shards",
    )
    parser.add_argument(
        "--start-ref",
        help="Commit/branch from which the benchmark base branch is created",
    )
    parser.add_argument("--base-branch", help="Benchmark PR base branch name")
    parser.add_argument("--pr-branch", help="Benchmark PR head branch name")
    parser.add_argument(
        "--fixture-root",
        default=DEFAULT_FIXTURE_ROOT,
        help="Path added inside the target repository",
    )
    parser.add_argument("--remote", default="origin", help="Git remote to push")
    parser.add_argument("--push", action="store_true", help="Push both branches")
    parser.add_argument(
        "--create-pr",
        action="store_true",
        help="Create the PR with GitHub CLI (requires --push and authenticated gh)",
    )
    parser.add_argument("--pr-title")
    parser.add_argument("--pr-body")
    parser.add_argument(
        "--no-verify",
        dest="verify",
        action="store_false",
        help="Skip compile and portable unittest verification",
    )
    parser.set_defaults(verify=True, handler=create_pr)


def preview_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "preview",
        help="Preview fixed-rule regrouping plans for an existing Git diff",
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--path", help="Optional Git pathspec")
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument("--minimum-total-weight", type=int, default=600)
    parser.add_argument("--target-weight", type=int, default=600)
    parser.add_argument("--seed", default="20260804")
    parser.add_argument(
        "--method",
        choices=[
            "all",
            "split-by-single-file",
            "random",
            "directory-distance",
            "lpt",
        ],
        default="all",
    )
    parser.add_argument("--output", help="Optional JSON output path")
    parser.set_defaults(handler=preview_command)


def rerun_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "rerun",
        help="Create an empty commit to retrigger the same PR without changing its diff",
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--branch", help="PR head branch to switch to")
    parser.add_argument(
        "--method",
        choices=[
            "split-by-single-file",
            "random",
            "directory-distance",
            "model",
            "lpt",
        ],
        required=True,
    )
    parser.add_argument("--model", help="Free-form model label for the commit message")
    parser.add_argument("--run", type=int, required=True)
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--push", action="store_true")
    parser.set_defaults(handler=rerun_command)


def env_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "env",
        help="Print worker environment variables for one benchmark configuration",
    )
    parser.add_argument(
        "--method",
        choices=[
            "split-by-single-file",
            "random",
            "directory-distance",
            "model",
            "lpt",
        ],
        required=True,
    )
    parser.add_argument(
        "--runtime",
        choices=["current", "claude-code", "codex"],
        default="current",
    )
    parser.add_argument("--model-id")
    parser.add_argument("--base-url")
    parser.add_argument("--parallel", type=int, default=8)
    parser.add_argument("--seed", default="20260804")
    parser.add_argument("--minimum-total-weight", type=int, default=600)
    parser.add_argument("--target-weight", type=int, default=600)
    parser.add_argument("--model-timeout", type=int, default=120)
    parser.set_defaults(handler=env_command)


def validate_plan_parser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "validate-plan",
        help="Inspect and validate review_reorganization_plan.json",
    )
    parser.add_argument("plan")
    parser.add_argument("--expected-method")
    parser.add_argument("--require-effective")
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="Fail when requested_method differs from effective_method",
    )
    parser.set_defaults(handler=validate_plan_command)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_parser(subparsers)
    preview_parser(subparsers)
    rerun_parser(subparsers)
    env_parser(subparsers)
    validate_plan_parser(subparsers)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
