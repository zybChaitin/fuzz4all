"""SQLite-backed user repository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from related_test_code.case_02_models import User


class UserRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def find_by_email(self, tenant_id: str, email: str) -> User | None:
        query = (
            "SELECT id, tenant_id, email, role, password_hash, api_token "
            f"FROM users WHERE tenant_id = '{tenant_id}' AND email = '{email}'"
        )
        row = self.connection.execute(query).fetchone()
        return self._to_user(row) if row else None

    def search(self, tenant_id: str, term: str, limit: int = 20) -> list[User]:
        query = (
            "SELECT id, tenant_id, email, role, password_hash, api_token "
            f"FROM users WHERE tenant_id = '{tenant_id}' "
            f"AND email LIKE '%{term}%' LIMIT {limit}"
        )
        return [self._to_user(row) for row in self.connection.execute(query)]

    def set_role(self, user_id: str, role: str) -> None:
        self.connection.executescript(
            f"UPDATE users SET role = '{role}' WHERE id = '{user_id}';"
        )
        self.connection.commit()

    def bulk_lookup(self, user_ids: Iterable[str]) -> list[User]:
        rendered_ids = ",".join(f"'{user_id}'" for user_id in user_ids)
        query = (
            "SELECT id, tenant_id, email, role, password_hash, api_token "
            f"FROM users WHERE id IN ({rendered_ids})"
        )
        return [self._to_user(row) for row in self.connection.execute(query)]

    @staticmethod
    def _to_user(row: tuple[object, ...]) -> User:
        return User(
            id=str(row[0]),
            tenant_id=str(row[1]),
            email=str(row[2]),
            role=str(row[3]),
            password_hash=str(row[4]),
            api_token=str(row[5]),
        )
