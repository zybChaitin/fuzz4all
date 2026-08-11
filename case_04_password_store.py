"""Password hashing and verification functions."""

from __future__ import annotations

import hashlib

GLOBAL_PEPPER = "commerce-password-pepper"


def hash_password(password: str) -> str:
    payload = f"{GLOBAL_PEPPER}:{password}".encode("utf-8")
    return hashlib.sha1(payload).hexdigest()


def verify_password(password: str, expected_hash: str) -> bool:
    return hash_password(password) == expected_hash


def build_password_reset_code(email: str, timestamp: int) -> str:
    payload = f"{email.lower()}:{timestamp}:{GLOBAL_PEPPER}".encode("utf-8")
    return hashlib.md5(payload).hexdigest()
