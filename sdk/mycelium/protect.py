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

import asyncio
import functools
import time
from contextvars import ContextVar
from typing import Any, Callable
from uuid import uuid4

_DEFAULT_TTL = 300  # seconds


class TenancyMismatchError(Exception):
    """Raised when a tool response does not contain the expected entity value.

    This catches DB-routing or proxy bugs where the response looks structurally
    valid but belongs to the wrong tenant, customer, or shard.
    """

    def __init__(self, expected: Any, actual: Any, field: str) -> None:
        super().__init__(
            f"Tenancy mismatch: expected {field}={expected!r}, got {actual!r}. "
            f"The response likely belongs to a different tenant/shard."
        )
        self.expected = expected
        self.actual = actual
        self.field = field


def _extract_field(result: Any, field: str) -> Any:
    """Pull *field* out of a dict or object."""
    if isinstance(result, dict):
        return result.get(field)
    return getattr(result, field, None)


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
        entity_field: str | None,
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

        if entity_field is not None and entity_id is not None:
            actual = _extract_field(result, entity_field)
            if actual != entity_id:
                self._cache.pop(key, None)
                self._audit.append({
                    "event": "cache_error",
                    "tool": tool_name,
                    "entity_id": entity_id,
                    "error": f"tenancy_mismatch(expected={entity_id!r}, got={actual!r})",
                    "ts": now,
                })
                raise TenancyMismatchError(
                    expected=entity_id, actual=actual, field=entity_field
                )

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
    entity_field: str | None = None,
    ttl: float = _DEFAULT_TTL,
    critical: bool = False,
) -> Callable:
    """
    Decorator that adds context protection to any async tool function.

    Args:
        entity_param: kwarg name that identifies the entity (e.g. "customer_id").
                      Different entity values get separate cache entries.
        entity_field: Optional field name inside the tool response to verify
                      round-trip correctness. If set and the returned value
                      does not match the entity_param value, TenancyMismatchError
                      is raised (cache cleared, agent can retry).
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
                result = await func(*args, **kwargs)
                if entity_field is not None and entity_param is not None:
                    entity_id = kwargs.get(entity_param)
                    actual = _extract_field(result, entity_field)
                    if actual != entity_id:
                        raise TenancyMismatchError(
                            expected=entity_id, actual=actual, field=entity_field
                        )
                return result
            session = _get_session()
            return await session.call(
                func=func,
                tool_name=tool_name,
                entity_param=entity_param,
                entity_field=entity_field,
                ttl=ttl,
                args=args,
                kwargs=kwargs,
            )

        wrapper._mycelium_protected = True
        wrapper._mycelium_entity_param = entity_param
        wrapper._mycelium_ttl = ttl
        return wrapper

    return decorator


def protect_sync(
    entity_param: str | None = None,
    entity_field: str | None = None,
    ttl: float = _DEFAULT_TTL,
    critical: bool = False,
) -> Callable:
    """
    Decorator for synchronous tool functions (e.g. smolagents Tool.forward).

    Behaves identically to @protect but wraps a sync function. Uses the
    current thread's event loop if one exists, otherwise creates a new one.

        @protect_sync(entity_param="customer_id", ttl=60)
        def fetch_customer(customer_id: str) -> dict:
            return db.get(customer_id)
    """
    def decorator(func: Callable) -> Callable:
        tool_name = func.__name__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if critical:
                result = func(*args, **kwargs)
                if entity_field is not None and entity_param is not None:
                    entity_id = kwargs.get(entity_param)
                    actual = _extract_field(result, entity_field)
                    if actual != entity_id:
                        raise TenancyMismatchError(
                            expected=entity_id, actual=actual, field=entity_field
                        )
                return result

            session = _get_session()
            entity_id = kwargs.get(entity_param) if entity_param else None
            key = session._key(tool_name, entity_id)
            now = time.monotonic()

            entry = session._cache.get(key)
            if entry is not None and now < entry.expires_at:
                session._audit.append({
                    "event": "cache_hit",
                    "tool": tool_name,
                    "entity_id": entity_id,
                    "ts": now,
                })
                return entry.value

            if entry is not None:
                session._audit.append({
                    "event": "cache_stale",
                    "tool": tool_name,
                    "entity_id": entity_id,
                    "ts": now,
                })

            try:
                result = func(*args, **kwargs)
            except Exception as exc:
                session._cache.pop(key, None)
                session._audit.append({
                    "event": "cache_error",
                    "tool": tool_name,
                    "entity_id": entity_id,
                    "error": str(exc),
                    "ts": now,
                })
                raise

            if entity_field is not None and entity_id is not None:
                actual = _extract_field(result, entity_field)
                if actual != entity_id:
                    session._cache.pop(key, None)
                    session._audit.append({
                        "event": "cache_error",
                        "tool": tool_name,
                        "entity_id": entity_id,
                        "error": f"tenancy_mismatch(expected={entity_id!r}, got={actual!r})",
                        "ts": now,
                    })
                    raise TenancyMismatchError(
                        expected=entity_id, actual=actual, field=entity_field
                    )

            session._cache[key] = _CacheEntry(
                value=result,
                expires_at=now + ttl,
                entity_id=entity_id,
                tool_name=tool_name,
            )
            session._audit.append({
                "event": "cache_add",
                "tool": tool_name,
                "entity_id": entity_id,
                "ts": now,
            })
            return result

        wrapper._mycelium_protected = True
        wrapper._mycelium_entity_param = entity_param
        wrapper._mycelium_ttl = ttl
        return wrapper

    return decorator
