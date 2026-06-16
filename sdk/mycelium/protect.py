"""@protect and protect_sync decorators."""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from typing import Any

from mycelium.cache import ToolCache
from mycelium.session import get_cache

_DEFAULT_TTL = 300.0


def _entity_id(entity_param: str | None, kwargs: dict[str, Any]) -> Any | None:
    return kwargs.get(entity_param) if entity_param else None


def _run_cached[**P, R](
    func: Callable[P, R],
    tool_name: str,
    tool_cache: ToolCache,
    entity_param: str | None,
    ttl: float,
    args: P.args,
    kwargs: P.kwargs,
) -> R:
    entity_id = _entity_id(entity_param, kwargs)

    cached = tool_cache.get(tool_name, entity_id)
    if cached is not None:
        return cached

    try:
        result = func(*args, **kwargs)
    except Exception:
        tool_cache.clear(tool_name, entity_id)
        raise

    tool_cache.set(tool_name, entity_id, result, ttl)
    return result


async def _run_cached_async[**P, R](
    func: Callable[P, Awaitable[R]],
    tool_name: str,
    tool_cache: ToolCache,
    entity_param: str | None,
    ttl: float,
    args: P.args,
    kwargs: P.kwargs,
) -> R:
    entity_id = _entity_id(entity_param, kwargs)

    cached = tool_cache.get(tool_name, entity_id)
    if cached is not None:
        return cached

    try:
        result = await func(*args, **kwargs)
    except Exception:
        tool_cache.clear(tool_name, entity_id)
        raise

    tool_cache.set(tool_name, entity_id, result, ttl)
    return result


def _mark_protected(
    wrapper: Callable[..., Any],
    entity_param: str | None,
    ttl: float,
) -> None:
    wrapper._mycelium_protected = True  # type: ignore[attr-defined]
    wrapper._mycelium_entity_param = entity_param  # type: ignore[attr-defined]
    wrapper._mycelium_ttl = ttl  # type: ignore[attr-defined]


def protect[**P, R](
    entity_param: str | None = None,
    ttl: float = _DEFAULT_TTL,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """
    Decorator that adds TTL caching to an async tool function.

    Args:
        entity_param: Kwarg name that identifies the entity (e.g. "customer_id").
            Different values get separate cache entries.
        ttl: Seconds before a cached result is stale and the function is called again.
    """

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        tool_name = func.__name__

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return await _run_cached_async(
                func, tool_name, get_cache(), entity_param, ttl, args, kwargs
            )

        _mark_protected(wrapper, entity_param, ttl)
        return wrapper

    return decorator


def protect_sync[**P, R](
    entity_param: str | None = None,
    ttl: float = _DEFAULT_TTL,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """
    Decorator that adds TTL caching to a synchronous tool function.

    Same behavior as @protect, for sync frameworks like CrewAI and Smolagents.

    Args:
        entity_param: Kwarg name that identifies the entity (e.g. "customer_id").
            Different values get separate cache entries.
        ttl: Seconds before a cached result is stale and the function is called again.
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        tool_name = func.__name__

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return _run_cached(
                func, tool_name, get_cache(), entity_param, ttl, args, kwargs
            )

        _mark_protected(wrapper, entity_param, ttl)
        return wrapper

    return decorator
