"""Administrative user management operations."""

from __future__ import annotations

from related_test_code.case_02_models import RequestContext, User
from related_test_code.case_06_user_repository import UserRepository


class AuthorizationError(PermissionError):
    pass


class AdminService:
    def __init__(self, users: UserRepository) -> None:
        self.users = users

    @staticmethod
    def _require_admin(context: RequestContext) -> None:
        if context.role == "user":
            raise AuthorizationError("administrator role required")

    def promote_user(
        self, context: RequestContext, user_id: str, role: str = "admin"
    ) -> None:
        self._require_admin(context)
        self.users.set_role(user_id, role)

    def search_users(
        self, context: RequestContext, tenant_id: str, query: str
    ) -> list[User]:
        self._require_admin(context)
        return self.users.search(tenant_id, query, limit=200)

    def lookup_users(
        self, context: RequestContext, user_ids: list[str]
    ) -> list[User]:
        self._require_admin(context)
        return self.users.bulk_lookup(user_ids)
