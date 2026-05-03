"""
Decorators for marking tools with context corruption protection rules.

Developers use these to declare:
- Which parameters are entity IDs (for segmentation)
- Which results are critical (require re-verification)
- How long results stay fresh (TTL)
- What counts as a rate-limit error

This metadata is read by the runtime to enforce context invalidation rules.
"""

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any


@dataclass
class ToolMetadata:
    """Metadata attached to a tool function by @tool decorator."""

    func_name: str
    critical: bool = False
    invalidate_after_steps: int = 5
    entity_param: str | None = None
    rate_limit_pattern: str | None = None


def tool(
    critical: bool = False,
    invalidate_after_steps: int = 5,
    entity_param: str | None = None,
    rate_limit_pattern: str | None = None,
) -> Callable:
    """
    Mark a tool function with context corruption protection rules.

    This decorator stores metadata that the runtime uses to:
    1. Extract entity_id from tool parameters (for segmentation)
    2. Mark results as HIGH/LOW criticality
    3. Set TTL for cached results
    4. Detect rate-limit errors specific to this tool

    Args:
        critical: If True, result is marked HIGH criticality.
                 Runtime will force re-verify if read 2+ times.
                 Use for user IDs, resource handles, API credentials, etc.
                 Default: False

        invalidate_after_steps: How many agent reasoning steps before this
                               result becomes stale and must be re-fetched.
                               Default: 5 steps
                               Set to 1 for always-fresh data (expensive)
                               Set to 999 for rarely-changing data

        entity_param: Name of the parameter that contains the entity_id.
                     Used for segmentation to prevent cross-entity leakage.
                     Example: entity_param="user_id" for fetch_user_data(user_id)
                     If None, result is treated as globally scoped.
                     Default: None (global scope)

        rate_limit_pattern: Regex pattern to detect rate-limit errors.
                           Used to distinguish retryable errors from failures.
                           If error matches this pattern, runtime can retry.
                           If None, inherits default from InvalidationPolicy.
                           Default: None

    Examples:
        # Fetching a user's profile: critical, entity-scoped, fresh every 5 steps
        @tool(critical=True, entity_param="user_id", invalidate_after_steps=5)
        def fetch_user(user_id: str) -> dict:
            return api.get(f"/users/{user_id}")

        # Searching documents: non-critical, global scope, fresh every 10 steps
        @tool(invalidate_after_steps=10)
        def search_docs(query: str) -> list[dict]:
            return db.search(query)

        # Fetching API quotas: critical, always fresh
        @tool(critical=True, invalidate_after_steps=1)
        def get_quota() -> dict:
            return api.get("/quota")

        # Listing users with custom rate-limit detection
        @tool(
            invalidate_after_steps=5,
            rate_limit_pattern=r"quota.?exceeded|API.?limit",
        )
        def list_users() -> list[dict]:
            return api.get("/users")
    """

    def decorator(func: Callable) -> Callable:
        import inspect

        # Validate inputs
        if not isinstance(critical, bool):
            raise TypeError(f"critical must be bool, got {type(critical)}")

        if not isinstance(invalidate_after_steps, int) or invalidate_after_steps < 1:
            raise ValueError(
                f"invalidate_after_steps must be int >= 1, got {invalidate_after_steps}"
            )

        if entity_param is not None and not isinstance(entity_param, str):
            raise TypeError(f"entity_param must be str or None, got {type(entity_param)}")

        if rate_limit_pattern is not None and not isinstance(rate_limit_pattern, str):
            raise TypeError(
                f"rate_limit_pattern must be str or None, got {type(rate_limit_pattern)}"
            )

        # Attach metadata to function
        metadata = ToolMetadata(
            func_name=func.__name__,
            critical=critical,
            invalidate_after_steps=invalidate_after_steps,
            entity_param=entity_param,
            rate_limit_pattern=rate_limit_pattern,
        )
        func._mycelium_tool_metadata = metadata

        # Preserve original function signature and docstring
        # Handle both async and sync functions
        if inspect.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args, **kwargs):
                return await func(*args, **kwargs)

            async_wrapper._mycelium_tool_metadata = metadata
            return async_wrapper
        else:

            @wraps(func)
            def sync_wrapper(*args, **kwargs):
                return func(*args, **kwargs)

            sync_wrapper._mycelium_tool_metadata = metadata
            return sync_wrapper

    return decorator


def protect(failure_mode: str) -> Callable:
    """
    Mark a function or coroutine to be protected against a specific failure mode.

    This is a top-level decorator that wraps the tool invocation with runtime
    enforcement. The runtime uses this to:
    1. Intercept tool calls
    2. Check cache state before calling
    3. Decide if fresh data is needed
    4. Log all access/invalidation decisions

    Args:
        failure_mode: String identifier of the failure mode.
                     Example: "context_corruption", "loop_detection", etc.
                     Must match a protection module in the runtime.

    Usage:
        @protect(failure_mode="context_corruption")
        async def my_agent_step():
            result = await fetch_user(user_id="123")
            # Runtime intercepts fetch_user, checks cache, enforces invalidation
            ...

    Note: This decorator is typically applied at the agent function level,
          not individual tools. Individual tools use @tool instead.
    """

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # The actual enforcement happens in the runtime,
            # this just marks the function for instrumentation
            return func(*args, **kwargs)

        wrapper._mycelium_protect_failure_mode = failure_mode
        return wrapper

    return decorator


class ToolRegistry:
    """
    Registry of all @tool-decorated functions in the codebase.

    The runtime uses this to:
    1. Look up metadata for any tool
    2. Extract entity_ids from arguments
    3. Determine cache keys
    4. Apply invalidation rules

    Usage:
        registry = ToolRegistry()
        registry.register(fetch_user)
        registry.register(search_docs)

        metadata = registry.get("fetch_user")
        entity_id = registry.extract_entity_id("fetch_user", {"user_id": "123"})
    """

    def __init__(self):
        self._tools: dict[str, ToolMetadata] = {}

    def register(self, func: Callable) -> None:
        """Register a @tool-decorated function."""
        if not hasattr(func, "_mycelium_tool_metadata"):
            raise ValueError(f"Function {func.__name__} is not decorated with @tool")
        metadata = func._mycelium_tool_metadata
        self._tools[func.__name__] = metadata

    def get(self, tool_name: str) -> ToolMetadata | None:
        """Get metadata for a tool by name."""
        return self._tools.get(tool_name)

    def extract_entity_id(self, tool_name: str, kwargs: dict[str, Any]) -> str | None:
        """
        Extract entity_id from tool call arguments.

        Args:
            tool_name: Name of the tool
            kwargs: Keyword arguments passed to the tool

        Returns:
            The entity_id value, or None if not applicable

        Example:
            registry.extract_entity_id("fetch_user", {"user_id": "alice", "include_profile": True})
            # Returns: "alice"
        """
        metadata = self.get(tool_name)
        if not metadata or not metadata.entity_param:
            return None

        return kwargs.get(metadata.entity_param)

    def list_all(self) -> dict[str, ToolMetadata]:
        """Return all registered tools."""
        return dict(self._tools)
