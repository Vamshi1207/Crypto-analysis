"""Small thread-safe TTL cache.

Free-tier quotas are the binding constraint (Helius 10 rps, Birdeye 1 rps), and
the same mint gets re-checked constantly while a chart is open. Caching by
namespace with per-namespace TTLs keeps us inside those limits.

In-process on purpose: a single Flask process holds all the state today. Swap
the backend for Valkey when the pipeline grows worker processes.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from cachetools import TTLCache

_DEFAULT_TTLS = {
    # Authorities change rarely and only via an on-chain transaction.
    "mint_account": 300.0,
    "holders": 60.0,
    # Price and liquidity move constantly; keep this short.
    "dexscreener": 15.0,
    "dexscreener_pair": 60.0,
    # Pool→mint mapping is effectively permanent for a given pool address.
    "resolve_mint": 3600.0,
    "jupiter_quote": 20.0,
}

_MAX_ENTRIES = 512

_caches: dict[str, TTLCache] = {}
_lock = threading.Lock()


def _cache_for(namespace: str) -> TTLCache:
    with _lock:
        cache = _caches.get(namespace)
        if cache is None:
            ttl = _DEFAULT_TTLS.get(namespace, 30.0)
            cache = TTLCache(maxsize=_MAX_ENTRIES, ttl=ttl)
            _caches[namespace] = cache
        return cache


def get_or_set(namespace: str, key: str, producer: Callable[[], Any]) -> Any:
    """Return the cached value for `key`, otherwise compute and store it.

    The producer runs outside the lock, so two concurrent misses on the same key
    may both call it. That is an acceptable trade for not serialising every
    network call behind one mutex.
    """
    cache = _cache_for(namespace)
    try:
        return cache[key]
    except KeyError:
        pass

    value = producer()
    cache[key] = value
    return value


def peek(namespace: str, key: str) -> Optional[Any]:
    return _cache_for(namespace).get(key)


def invalidate(namespace: Optional[str] = None) -> None:
    with _lock:
        targets = [namespace] if namespace else list(_caches)
        for name in targets:
            if name in _caches:
                _caches[name].clear()


def stats() -> dict[str, int]:
    with _lock:
        return {name: len(cache) for name, cache in _caches.items()}
