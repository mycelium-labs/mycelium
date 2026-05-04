"""
@protect decorator — primary Mycelium API.

Wrap any async tool function once. It works transparently in LangGraph,
AutoGen, CrewAI, or any other framework without changing how you call it.

Usage:
    from mycelium import protect

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return await db.get(customer_id)

    # Use normally — Mycelium intercepts automatically
    result = await fetch_customer(customer_id="c1")
"""

import functools
import time
from contextvars import ContextVar
from typing import Any, Callable
from uuid import uuid4

_DEFAULT_TTL = 300  # seconds

# Per-async-context session, falls back to a global session
_session_var: ContextVar["Session"] = ContextVar("mycelium_session")
_global_session: "Session | None" = None


def _get_session() -> "Session":
    global _global_session
    try:
        return _session_var.get()
    except LookupError:
        if _global_session is None:
            _global_session = Session()
        return _global_session


class _CacheEntry:
    __slots__ = ("value", "expires_at", "entity_id", "tool_name")

    def __init__(self, value: Any, expires_at: float, entity_id: str | None, tool_name: str):
        self.value = value
        self.expires_at = expires_at
        self.entity_id = entity_id
        self.tool_name = tool_name


class Session:
    """
    Isolated cache scope. Create one per agent run to prevent cross-run leakage.

        async with mycelium.Session() as session:
            result = await fetch_customer(customer_id="c1")
    """

    def __init__(self) -> None:
        self._cache: dict[str, _CacheEntry] = {}
        self._audit: list[dict[str, Any]] = []

    def _key(self, tool_name: str, entity_id: str | None) -> str:
        return f"{tool_name}:{entity_id}" if entity_id is not None else tool_name

    async def call(
        self,
        func: Callable,
        tool_name: str,
        entity_param: str | None,
        ttl: float,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        entity_id = kwargs.get(entity_param) if entity_param else None
        key = self._key(tool_name, entity_id)
        now = time.monotonic()

        entry = self._cache.get(key)
        if entry is not None and now < entry.expires_at:
            self._audit.append({
                "event": "cache_hit",
                "tool": tool_name,
                "entity_id": entity_id,
                "ts": now,
            })
            return entry.value

        if entry is not None:
            self._audit.append({
                "event": "cache_stale",
                "tool": tool_name,
                "entity_id": entity_id,
                "ts": now,
            })

        try:
            result = await func(*args, **kwargs)
        except Exception as exc:
            self._cache.pop(key, None)
            self._audit.append({
                "event": "cache_error",
                "tool": tool_name,
                "entity_id": entity_id,
                "error": str(exc),
                "ts": now,
            })
            raise

        self._cache[key] = _CacheEntry(
            value=result,
            expires_at=now + ttl,
            entity_id=entity_id,
            tool_name=tool_name,
        )
        self._audit.append({
            "event": "cache_add",
            "tool": tool_name,
            "entity_id": entity_id,
            "ts": now,
        })
        return result

    def invalidate(self, tool_name: str, entity_id: str | None = None) -> None:
        key = self._key(tool_name, entity_id)
        self._cache.pop(key, None)

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)

    def cache_size(self) -> int:
        now = time.monotonic()
        return sum(1 for e in self._cache.values() if now < e.expires_at)

    async def __aenter__(self) -> "Session":
        self._token = _session_var.set(self)
        return self

    async def __aexit__(self, *_: Any) -> None:
        _session_var.reset(self._token)


def protect(
    entity_param: str | None = None,
    ttl: float = _DEFAULT_TTL,
    critical: bool = False,
) -> Callable:
    """
    Decorator that adds context protection to any async tool function.

    Args:
        entity_param: kwarg name that identifies the entity (e.g. "customer_id").
                      Different entity values get separate cache entries.
        ttl:          Seconds before a cached result is considered stale and
                      the real function is called again. Default: 300s.
        critical:     If True, always refetch (no caching). Reserved for
                      tools where staleness is never acceptable.
    """
    def decorator(func: Callable) -> Callable:
        tool_name = func.__name__

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            if critical:
                return await func(*args, **kwargs)
            session = _get_session()
            return await session.call(
                func=func,
                tool_name=tool_name,
                entity_param=entity_param,
                ttl=ttl,
                args=args,
                kwargs=kwargs,
            )

        wrapper._mycelium_protected = True
        wrapper._mycelium_entity_param = entity_param
        wrapper._mycelium_ttl = ttl
        return wrapper

    return decorator
