"""
Runtime integration for AF-006 context corruption protection.

This module shows how ContextCache is wired into the agent execution loop.

The runtime intercepts all tool calls and enforces context invalidation rules:
1. Before calling a tool: Check cache, decide if refetch is needed
2. After tool returns: Store result with metadata
3. On tool error: Invalidate related context
4. After each reasoning step: Advance step counter

The developer writes normal agent code. The runtime handles caching transparently.
"""

import asyncio
import inspect
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from mycelium.protections.context_corruption import (
    ContextCache,
    ContextSegmentation,
    Criticality,
    InvalidationPolicy,
)
from mycelium.protections.decorators import ToolMetadata, ToolRegistry


class RefetchAction(Enum):
    """What the runtime should do when a tool is called."""
    USE_CACHE = "use_cache"
    REFETCH = "refetch"
    NOT_CACHED = "not_cached"


@dataclass
class ToolCallContext:
    """Context for a single tool invocation."""
    tool_name: str
    tool_func: Callable
    tool_kwargs: dict[str, Any]
    metadata: ToolMetadata | None
    entity_id: str | None
    cached_value: Any | None
    action: RefetchAction
    reason: str


class AgentRuntimeWithContextProtection:
    """
    Agent runtime with AF-006 context corruption protection.

    Architecture:
    1. Registry: Maps tool names to their metadata
    2. Cache: Stores tool results with TTL/criticality rules
    3. Interceptor: Wraps tool calls to enforce caching
    4. Loop: Manages step advancement and error handling

    Usage:
        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([fetch_user, search_docs, get_quota])

        result = await runtime.call_tool("fetch_user", user_id="alice")
        runtime.advance_step()
    """

    def __init__(
        self,
        policy: InvalidationPolicy | None = None,
        verbose: bool = False,
    ):
        if policy is None:
            policy = InvalidationPolicy(
                default_ttl_steps=5,
                criticality_recheck_threshold=2,
                segmentation=ContextSegmentation.BOTH,
            )

        self.policy = policy
        self.cache = ContextCache(policy)
        self.registry = ToolRegistry()
        self.verbose = verbose
        self.current_step = 0

    def register_tools(self, tools: list[Callable]) -> None:
        """Register @tool-decorated functions."""
        for tool in tools:
            if hasattr(tool, "_mycelium_tool_metadata"):
                self.registry.register(tool)
                if self.verbose:
                    print(f"[RUNTIME] Registered tool: {tool.__name__}")
            else:
                if self.verbose:
                    print(f"[RUNTIME] Warning: {tool.__name__} not decorated with @tool")

    async def call_tool(
        self,
        tool_name: str,
        tool_func: Callable,
        **tool_kwargs,
    ) -> Any:
        """
        Call a tool with context corruption protection.

        FLOW:
        1. Look up tool metadata in registry
        2. Check cache (if registered)
        3. Decide: use cache / refetch / not cached
        4. Call tool (or return cached value)
        5. On success: store in cache
        6. On error: invalidate related context
        7. Return value to agent

        Args:
            tool_name: Name of the tool (must match function name)
            tool_func: The actual tool function to call
            **tool_kwargs: Arguments to pass to the tool

        Returns:
            The tool result (from cache or fresh call)

        Raises:
            Original exception if tool fails
        """
        # Step 1: Look up metadata
        metadata = self.registry.get(tool_name)
        if not metadata and self.verbose:
            print(f"[RUNTIME] Warning: {tool_name} not in registry, no caching")

        # Step 2: Extract entity_id (if applicable)
        entity_id = None
        if metadata and metadata.entity_param:
            entity_id = tool_kwargs.get(metadata.entity_param)

        # Step 3: Check cache
        ctx = ToolCallContext(
            tool_name=tool_name,
            tool_func=tool_func,
            tool_kwargs=tool_kwargs,
            metadata=metadata,
            entity_id=entity_id,
            cached_value=None,
            action=RefetchAction.NOT_CACHED,
            reason="No metadata",
        )

        if metadata:
            decision = self.cache.get(
                name=tool_name,
                source=tool_name,
                entity_id=entity_id,
            )

            ctx.cached_value = decision.value
            ctx.reason = decision.reason

            if decision.should_refetch:
                ctx.action = RefetchAction.REFETCH
            else:
                ctx.action = RefetchAction.USE_CACHE

            if self.verbose:
                self._log_cache_decision(ctx, decision)

        # Step 4: Execute or return cache
        if ctx.action == RefetchAction.USE_CACHE:
            if self.verbose:
                print("  → Using cached value")
            return ctx.cached_value

        # Fresh fetch needed
        if self.verbose:
            print(f"  → Calling tool (reason: {ctx.reason})")

        try:
            result = await self._call_tool_async(tool_func, tool_kwargs)
        except Exception as error:
            # Step 6: Error handling
            if self.verbose:
                print(f"  → Tool failed: {error}")

            if metadata:
                is_rate_limit = self.cache.invalidate_on_error(
                    source=tool_name,
                    error=error,
                    entity_id=entity_id,
                )

                if is_rate_limit and self.verbose:
                    print("  → Rate-limit error detected")

            raise

        # Step 5: Cache the result
        if metadata:
            version_id = self.cache.add(
                name=tool_name,
                value=result,
                source=tool_name,
                entity_id=entity_id,
                criticality=Criticality.HIGH if metadata.critical else Criticality.LOW,
                invalidate_after_steps=metadata.invalidate_after_steps,
            )
            if self.verbose:
                print(f"  → Cached (version={version_id[:8]}...)")

        return result

    def advance_step(self) -> None:
        """
        Called after agent completes one reasoning step.

        This increments the step counter, which triggers TTL checks on
        the next cache access.

        Call this in your agent loop after each reasoning cycle:

            for reasoning_step in range(max_steps):
                decision = await agent.reason()
                result = await runtime.call_tool(...)
                runtime.advance_step()
        """
        self.cache.advance_step()
        self.current_step += 1
        if self.verbose:
            print(f"\n[STEP {self.current_step}]")

    def get_cache_snapshot(self) -> dict[str, Any]:
        """Get current cache state (for debugging)."""
        return self.cache.get_state_snapshot()

    def get_audit_log(self) -> list[dict[str, Any]]:
        """Get complete audit trail of cache operations."""
        return self.cache.get_audit_log()

    async def _call_tool_async(self, tool_func: Callable, kwargs: dict) -> Any:
        """Call tool function, handling both sync and async."""
        if inspect.iscoroutinefunction(tool_func):
            return await tool_func(**kwargs)
        else:
            # Run sync function in thread pool to avoid blocking
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, lambda: tool_func(**kwargs))

    def _log_cache_decision(self, ctx: ToolCallContext, decision) -> None:
        """Log cache decision for visibility."""
        status = "HIT" if ctx.action == RefetchAction.USE_CACHE else "MISS/REFETCH"
        print(
            f"[CACHE {status}] {ctx.tool_name}(entity={ctx.entity_id}) "
            f"age={decision.age_steps} steps, "
            f"access={decision.access_count}x, "
            f"reason: {decision.reason}"
        )


class AgentExecutor:
    """
    High-level executor for agents with context protection.

    This shows the typical agent loop:
    1. Reasoning step
    2. Tool calls
    3. Step advancement
    4. Error handling

    Usage:
        executor = AgentExecutor(agent_func, runtime)
        result = await executor.run(max_steps=20)
    """

    def __init__(
        self,
        agent_func: Callable,
        runtime: AgentRuntimeWithContextProtection,
    ):
        self.agent_func = agent_func
        self.runtime = runtime

    async def run(self, max_steps: int = 20) -> Any:
        """
        Run agent with context protection.

        The agent function receives the runtime as context:

            async def my_agent(runtime: AgentRuntimeWithContextProtection):
                for step in range(20):
                    decision = await my_reasoning_logic()

                    if decision == "fetch_user":
                        user = await runtime.call_tool(
                            "fetch_user",
                            fetch_user,
                            user_id="alice"
                        )

                    runtime.advance_step()

                return result
        """
        return await self.agent_func(self.runtime)


# ============================================================================
# Example: Complete agent loop with context protection
# ============================================================================


async def example_protected_agent(runtime: AgentRuntimeWithContextProtection):
    """
    Example agent that uses the protected runtime.

    Demonstrates:
    1. Calling tools through the runtime
    2. Step advancement
    3. Error handling
    4. Cache hits/misses
    """
    from examples.context_corruption_usage import (
        fetch_user_profile,
        get_api_quota,
        search_documents,
    )

    runtime.register_tools([fetch_user_profile, search_documents, get_api_quota])

    user_id = "alice"

    print("=== Example Protected Agent ===\n")

    # Step 1: Fetch user
    print("[STEP 1] Fetch user profile")
    user = await runtime.call_tool("fetch_user_profile", fetch_user_profile, user_id=user_id)
    print(f"User: {user['name']} (status: {user['status']})")
    runtime.advance_step()

    # Step 2: Search docs
    print("\n[STEP 2] Search documents")
    docs = await runtime.call_tool("search_documents", search_documents, query="ML", max_results=3)
    print(f"Found {len(docs)} documents")
    runtime.advance_step()

    # Step 3: Check quota
    print("\n[STEP 3] Check quota")
    quota = await runtime.call_tool("get_api_quota", get_api_quota)
    print(f"Quota: {quota['requests_remaining']} remaining")
    runtime.advance_step()

    # Step 4: Fetch user again (cached, age=3)
    print("\n[STEP 4] Fetch user again (expect cache hit)")
    user2 = await runtime.call_tool("fetch_user_profile", fetch_user_profile, user_id=user_id)
    print(f"User: {user2['name']}")
    runtime.advance_step()

    # Step 5-8: Idle steps
    for i in range(4):
        print(f"\n[STEP {5+i}] (idle)")
        runtime.advance_step()

    # Step 9: Fetch user again (stale now, should refetch)
    print("\n[STEP 9] Fetch user again (expect cache miss, age=8 > TTL=5)")
    user3 = await runtime.call_tool("fetch_user_profile", fetch_user_profile, user_id=user_id)
    print(f"User: {user3['name']}")
    runtime.advance_step()

    print("\n\n=== AUDIT LOG ===")
    for event in runtime.get_audit_log():
        if event["event_type"] in ("add", "get_hit", "get_stale", "get_repeated_read"):
            print(
                f"{event['event_type']:20} | step {event['step']:2} | {event['data'].get('name', 'n/a')}"
            )

    return user3
