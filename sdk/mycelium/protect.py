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


def _is_empty_result(value: Any) -> bool:
    """Return True for values that should be treated as 'negative' / empty."""
    if value is None:
        return True
    if value == []:
        return True
    if value == {}:
        return True
    if value == "":
        return True
    return False


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
    __slots__ = ("value", "expires_at", "entity_id", "tool_name", "value_hash", "last_accessed")

    def __init__(self, value: Any, expires_at: float, entity_id: str | None, tool_name: str, last_accessed: float | None = None):
        self.value = value
        self.expires_at = expires_at
        self.entity_id = entity_id
        self.tool_name = tool_name
        self.value_hash = _value_hash(value)
        self.last_accessed = last_accessed


def _value_hash(value: Any) -> str:
    """Stable hash for variance detection."""
    try:
        import hashlib
        return hashlib.md5(str(value).encode(), usedforsecurity=False).hexdigest()
    except Exception:
        return ""


class Session:
    """
    Isolated cache scope. Create one per agent run to prevent cross-run leakage.

        async with mycelium.Session() as session:
            result = await fetch_customer(customer_id="c1")
    """

    def __init__(self, max_entries: int | None = None) -> None:
        self._cache: dict[str, _CacheEntry] = {}
        self._audit: list[dict[str, Any]] = []
        self._variance_window: dict[str, list[str]] = {}  # key -> last 5 value hashes
        self._writes: dict[str, float] = {}  # entity_id -> last write timestamp
        self._max_entries = max_entries

    def _record_variance(self, key: str, entry: _CacheEntry) -> None:
        """Track value changes for the same cache key. Warn if non-deterministic."""
        window = self._variance_window.setdefault(key, [])
        window.append(entry.value_hash)
        if len(window) > 5:
            window.pop(0)
        if len(window) >= 3:
            unique = len(set(window))
            if unique >= len(window) * 0.6:  # 60%+ variance = likely non-deterministic
                self._audit.append({
                    "event": "variance_warning",
                    "tool": entry.tool_name,
                    "entity_id": entry.entity_id,
                    "unique_values": unique,
                    "window_size": len(window),
                    "ts": time.monotonic(),
                })

    def _key(self, tool_name: str, entity_id: str | None) -> str:
        return f"{tool_name}:{entity_id}" if entity_id is not None else tool_name

    def _evict_if_needed(self, now: float) -> None:
        """If cache exceeds max_entries, evict the least-recently-used entry."""
        if self._max_entries is None:
            return
        live = {k: e for k, e in self._cache.items() if now < e.expires_at}
        if len(live) < self._max_entries:
            return
        # Find LRU entry among live ones
        lru_key = min(live, key=lambda k: live[k].last_accessed or 0)
        self._cache.pop(lru_key, None)
        self._audit.append({
            "event": "cache_evict_lru",
            "tool": live[lru_key].tool_name,
            "entity_id": live[lru_key].entity_id,
            "ts": now,
        })

    async def call(
        self,
        func: Callable,
        tool_name: str,
        entity_param: str | None,
        entity_field: str | None,
        ttl: float,
        cache_empty: float | None,
        deterministic: bool,
        mark_as_write: bool,
        read_after_write_grace: float,
        args: tuple,
        kwargs: dict,
    ) -> Any:
        entity_id = kwargs.get(entity_param) if entity_param else None
        key = self._key(tool_name, entity_id)
        now = time.monotonic()

        # Write grace: if this entity was recently written, force fresh read.
        if (
            entity_id is not None
            and read_after_write_grace > 0
            and entity_id in self._writes
        ):
            elapsed = now - self._writes[entity_id]
            if elapsed < read_after_write_grace:
                self._audit.append({
                    "event": "cache_write_grace_bypass",
                    "tool": tool_name,
                    "entity_id": entity_id,
                    "elapsed": round(elapsed, 3),
                    "grace": read_after_write_grace,
                    "ts": now,
                })
                result = await func(*args, **kwargs)
                if entity_field is not None and entity_id is not None:
                    actual = _extract_field(result, entity_field)
                    if actual != entity_id:
                        raise TenancyMismatchError(
                            expected=entity_id, actual=actual, field=entity_field
                        )
                # Cache the fresh result so future reads (after grace) can hit.
                self._evict_if_needed(now)
                self._cache[key] = _CacheEntry(
                    value=result,
                    expires_at=now + ttl,
                    entity_id=entity_id,
                    tool_name=tool_name,
                    last_accessed=now,
                )
                self._record_variance(key, self._cache[key])
                self._audit.append({
                    "event": "cache_add",
                    "tool": tool_name,
                    "entity_id": entity_id,
                    "ts": now,
                })
                return result

        entry = self._cache.get(key)
        if entry is not None and now < entry.expires_at:
            entry.last_accessed = now
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

        if not deterministic:
            self._audit.append({
                "event": "cache_skip",
                "tool": tool_name,
                "entity_id": entity_id,
                "reason": "non_deterministic",
                "ts": now,
            })
            return result

        if _is_empty_result(result) and cache_empty is not None:
            if cache_empty <= 0:
                self._audit.append({
                    "event": "cache_skip",
                    "tool": tool_name,
                    "entity_id": entity_id,
                    "reason": "negative_cache",
                    "ts": now,
                })
                return result
            effective_ttl = cache_empty
            self._evict_if_needed(now)
            self._cache[key] = _CacheEntry(
                value=result,
                expires_at=now + effective_ttl,
                entity_id=entity_id,
                tool_name=tool_name,
                last_accessed=now,
            )
            self._audit.append({
                "event": "cache_add_negative",
                "tool": tool_name,
                "entity_id": entity_id,
                "ttl": effective_ttl,
                "ts": now,
            })
            return result

        self._evict_if_needed(now)
        self._cache[key] = _CacheEntry(
            value=result,
            expires_at=now + ttl,
            entity_id=entity_id,
            tool_name=tool_name,
            last_accessed=now,
        )
        self._record_variance(key, self._cache[key])
        self._audit.append({
            "event": "cache_add",
            "tool": tool_name,
            "entity_id": entity_id,
            "ts": now,
        })

        if mark_as_write and entity_id is not None:
            self._writes[entity_id] = now
            self._audit.append({
                "event": "write_tracked",
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
    deterministic: bool = True,
    cache_empty: float | None = None,
    mark_as_write: bool = False,
    read_after_write_grace: float = 0.0,
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
        deterministic: If False, skip caching because the tool can return
                       different values for identical inputs (e.g. stock prices,
                       random draws, time-dependent data). The SDK still runs
                       entity_field validation and variance warnings.
        cache_empty:  Special TTL for "negative" / empty results (None, [], {}, "").
                      None  = empty results use the normal ttl (default).
                      <= 0  = never cache empty results (always refetch).
                      > 0   = empty results cached for this shorter TTL.
        mark_as_write: If True, record this call as a write operation. Subsequent
                       reads for the same entity within read_after_write_grace
                       seconds will bypass the cache and fetch fresh data.
        read_after_write_grace: Seconds after a write during which reads for the
                                same entity bypass the cache. Default 0 (disabled).
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
                if mark_as_write and entity_param is not None:
                    entity_id = kwargs.get(entity_param)
                    if entity_id is not None:
                        session = _get_session()
                        session._writes[entity_id] = time.monotonic()
                        session._audit.append({
                            "event": "write_tracked",
                            "tool": tool_name,
                            "entity_id": entity_id,
                            "ts": time.monotonic(),
                        })
                return result
            session = _get_session()
            return await session.call(
                func=func,
                tool_name=tool_name,
                entity_param=entity_param,
                entity_field=entity_field,
                ttl=ttl,
                cache_empty=cache_empty,
                deterministic=deterministic,
                mark_as_write=mark_as_write,
                read_after_write_grace=read_after_write_grace,
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
    deterministic: bool = True,
    cache_empty: float | None = None,
    mark_as_write: bool = False,
    read_after_write_grace: float = 0.0,
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
                if mark_as_write and entity_param is not None:
                    entity_id = kwargs.get(entity_param)
                    if entity_id is not None:
                        session = _get_session()
                        session._writes[entity_id] = time.monotonic()
                        session._audit.append({
                            "event": "write_tracked",
                            "tool": tool_name,
                            "entity_id": entity_id,
                            "ts": time.monotonic(),
                        })
                return result

            session = _get_session()
            entity_id = kwargs.get(entity_param) if entity_param else None
            key = session._key(tool_name, entity_id)
            now = time.monotonic()

            # Write grace: if this entity was recently written, force fresh read.
            if (
                entity_id is not None
                and read_after_write_grace > 0
                and entity_id in session._writes
            ):
                elapsed = now - session._writes[entity_id]
                if elapsed < read_after_write_grace:
                    session._audit.append({
                        "event": "cache_write_grace_bypass",
                        "tool": tool_name,
                        "entity_id": entity_id,
                        "elapsed": round(elapsed, 3),
                        "grace": read_after_write_grace,
                        "ts": now,
                    })
                    result = func(*args, **kwargs)
                    if entity_field is not None and entity_id is not None:
                        actual = _extract_field(result, entity_field)
                        if actual != entity_id:
                            raise TenancyMismatchError(
                                expected=entity_id, actual=actual, field=entity_field
                            )
                    session._evict_if_needed(now)
                    session._cache[key] = _CacheEntry(
                        value=result,
                        expires_at=now + ttl,
                        entity_id=entity_id,
                        tool_name=tool_name,
                        last_accessed=now,
                    )
                    session._record_variance(key, session._cache[key])
                    session._audit.append({
                        "event": "cache_add",
                        "tool": tool_name,
                        "entity_id": entity_id,
                        "ts": now,
                    })
                    return result

            entry = session._cache.get(key)
            if entry is not None and now < entry.expires_at:
                entry.last_accessed = now
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

            if not deterministic:
                session._audit.append({
                    "event": "cache_skip",
                    "tool": tool_name,
                    "entity_id": entity_id,
                    "reason": "non_deterministic",
                    "ts": now,
                })
                return result

            if _is_empty_result(result) and cache_empty is not None:
                if cache_empty <= 0:
                    session._audit.append({
                        "event": "cache_skip",
                        "tool": tool_name,
                        "entity_id": entity_id,
                        "reason": "negative_cache",
                        "ts": now,
                    })
                    return result
                effective_ttl = cache_empty
                session._evict_if_needed(now)
                session._cache[key] = _CacheEntry(
                    value=result,
                    expires_at=now + effective_ttl,
                    entity_id=entity_id,
                    tool_name=tool_name,
                    last_accessed=now,
                )
                session._audit.append({
                    "event": "cache_add_negative",
                    "tool": tool_name,
                    "entity_id": entity_id,
                    "ttl": effective_ttl,
                    "ts": now,
                })
                return result

            session._evict_if_needed(now)
            session._cache[key] = _CacheEntry(
                value=result,
                expires_at=now + ttl,
                entity_id=entity_id,
                tool_name=tool_name,
                last_accessed=now,
            )
            session._record_variance(key, session._cache[key])
            session._audit.append({
                "event": "cache_add",
                "tool": tool_name,
                "entity_id": entity_id,
                "ts": now,
            })

            if mark_as_write and entity_id is not None:
                session._writes[entity_id] = now
                session._audit.append({
                    "event": "write_tracked",
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
