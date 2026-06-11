#!/usr/bin/env python3
"""
store.py — shared key/value with TTL for the two pieces of state that must work
across multiple workers/instances: single-use login nonces and rate-limit
counters. Sessions stay stateless (HMAC), so they need no store.

Backends:
  • MemoryStore — default; fine for a single process.
  • RedisStore  — set REDIS_URL to scale horizontally (multi-worker / multi-host).

The interface is tiny on purpose: setex, getdel (atomic single-use), incr_window.
"""

from __future__ import annotations

import os
import time
import threading


class MemoryStore:
    def __init__(self):
        self._d: dict[str, tuple[str, float]] = {}
        self._counts: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def setex(self, key: str, ttl: int, value: str) -> None:
        with self._lock:
            self._d[key] = (value, time.time() + ttl)

    def get(self, key: str) -> str | None:
        """Read without consuming (relay payloads must survive webview relaunches)."""
        with self._lock:
            rec = self._d.get(key)
        if not rec:
            return None
        value, exp = rec
        return value if time.time() <= exp else None

    def delete(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)

    def getdel(self, key: str) -> str | None:
        with self._lock:
            rec = self._d.pop(key, None)
        if not rec:
            return None
        value, exp = rec
        return value if time.time() <= exp else None

    def incr_window(self, key: str, window: int) -> int:
        """Fixed-window counter: returns the count within the current window."""
        bucket = int(time.time() // window)
        k = f"{key}:{bucket}"
        with self._lock:
            count, exp = self._counts.get(k, (0, 0.0))
            now = time.time()
            if now > exp:
                count = 0
            count += 1
            self._counts[k] = (count, (bucket + 1) * window)
            # opportunistic cleanup
            if len(self._counts) > 10000:
                for ck in [c for c, (_, e) in self._counts.items() if now > e]:
                    self._counts.pop(ck, None)
        return count


class RedisStore:
    def __init__(self, url: str):
        import redis  # lazy import; only needed when REDIS_URL is set

        self._r = redis.Redis.from_url(url, decode_responses=True)

    def setex(self, key: str, ttl: int, value: str) -> None:
        self._r.setex(key, ttl, value)

    def get(self, key: str) -> str | None:
        return self._r.get(key)

    def delete(self, key: str) -> None:
        self._r.delete(key)

    def getdel(self, key: str) -> str | None:
        # GETDEL is atomic (Redis 6.2+); fall back to pipeline if unavailable.
        try:
            return self._r.getdel(key)
        except Exception:  # noqa: BLE001
            p = self._r.pipeline()
            p.get(key)
            p.delete(key)
            return p.execute()[0]

    def incr_window(self, key: str, window: int) -> int:
        bucket = int(time.time() // window)
        k = f"{key}:{bucket}"
        p = self._r.pipeline()
        p.incr(k)
        p.expire(k, window + 1)
        return int(p.execute()[0])


_store = None


def get_store():
    global _store
    if _store is None:
        url = os.environ.get("REDIS_URL", "").strip()
        _store = RedisStore(url) if url else MemoryStore()
    return _store


def backend_name() -> str:
    return "redis" if os.environ.get("REDIS_URL", "").strip() else "memory"
