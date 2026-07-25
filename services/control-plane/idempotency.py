"""Idempotency key store.

In-memory dict keyed by (idempotency_key, endpoint, workspace_id, body_hash).
On retry with same key+body: return stored (status, body).
On retry with same key but different body: 409 Conflict.
Keys expire after IDEMPOTENCY_TTL_SECONDS.
"""

import hashlib
import time
from typing import Optional

IDEMPOTENCY_TTL_SECONDS = 86400  # 24 hours


class IdempotencyStore:
    """Thread-safe (GIL) in-memory idempotency store with TTL eviction."""

    def __init__(self):
        self._store: dict[str, tuple[int, int, str]] = {}  # key -> (expires_at, status, body)

    def _make_key(self, idempotency_key: str, endpoint: str, workspace_id: str, body: Optional[bytes]) -> str:
        body_hash = hashlib.sha256(body or b"").hexdigest() if body else "none"
        raw = f"{idempotency_key}|{endpoint}|{workspace_id}|{body_hash}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def _make_lookup_key(self, idempotency_key: str, endpoint: str, workspace_id: str) -> str:
        """Key for looking up existing entries (ignoring body hash)."""
        raw = f"{idempotency_key}|{endpoint}|{workspace_id}|"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def get(self, idempotency_key: str, endpoint: str, workspace_id: str, body: Optional[bytes]) -> Optional[tuple[int, str]]:
        """Return (status, response_body) if a matching idempotent result exists, else None."""
        self._evict()
        key = self._make_key(idempotency_key, endpoint, workspace_id, body)
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, status, body_str = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return (status, body_str)

    def check_conflict(self, idempotency_key: str, endpoint: str, workspace_id: str, body: Optional[bytes]) -> bool:
        """Return True if the same key was used with a different body (409 conflict)."""
        self._evict()
        lookup = self._make_lookup_key(idempotency_key, endpoint, workspace_id)
        for k in list(self._store.keys()):
            if k.startswith(lookup) and k != self._make_key(idempotency_key, endpoint, workspace_id, body):
                return True
        return False

    def set(self, idempotency_key: str, endpoint: str, workspace_id: str, body: Optional[bytes], status: int, response_body: str):
        """Store the result for this idempotency key."""
        key = self._make_key(idempotency_key, endpoint, workspace_id, body)
        expires_at = time.monotonic() + IDEMPOTENCY_TTL_SECONDS
        self._store[key] = (expires_at, status, response_body)

    def _evict(self):
        now = time.monotonic()
        expired = [k for k, (exp, _, _) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]


# Singleton
idempotency_store = IdempotencyStore()
