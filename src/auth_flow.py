"""Small, dependency-free helpers for the embedded Yandex login flow."""

from __future__ import annotations

import json


def cookie_fingerprint(records: list[dict]) -> tuple:
    """Return an in-memory fingerprint without logging cookie values."""
    return tuple(
        sorted(
            (
                str(item.get("name", "")).lower(),
                str(item.get("value", "")),
                str(item.get("domain", "")).lower(),
                str(item.get("path", "")),
            )
            for item in records
            if item.get("name") and "yandex." in str(item.get("domain", "")).lower()
        )
    )


def safe_auth_error(error: Exception) -> str:
    """Describe an auth failure without including tokens, cookies or URLs."""
    current = error
    visited = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        code = getattr(current, "code", None)
        if isinstance(code, int):
            return f"HTTP {code}"
        current = current.__cause__ or current.__context__
    return type(error).__name__


class LoginCookieGate:
    """Wait for a stable Yandex cookie set and rate-limit token exchanges."""

    def __init__(self, stable_seconds=2.0, retry_seconds=12.0):
        self.stable_seconds = stable_seconds
        self.retry_seconds = retry_seconds
        self.candidate = None
        self.candidate_since = 0.0
        self.attempted_at = {}

    def should_attempt(self, records: list[dict], now: float) -> bool:
        names = {str(item.get("name", "")).lower() for item in records}
        fingerprint = cookie_fingerprint(records)
        if not {"session_id", "sessionid2"}.intersection(names) or not fingerprint:
            self.candidate = None
            self.candidate_since = 0.0
            return False
        if fingerprint != self.candidate:
            self.candidate = fingerprint
            self.candidate_since = now
            return False
        if now - self.candidate_since < self.stable_seconds:
            return False
        if now - self.attempted_at.get(fingerprint, float("-inf")) < self.retry_seconds:
            return False
        self.attempted_at[fingerprint] = now
        return True


def exchange_and_validate_cookie_records(records, token_from_cookies, list_stations):
    """Exchange settled cookies and prove the token against Smart Home first."""
    token = token_from_cookies(json.dumps(records, ensure_ascii=False))
    stations = list_stations(token)
    return token, stations
