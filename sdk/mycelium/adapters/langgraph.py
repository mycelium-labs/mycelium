"""
LangGraph Integration for AF-006 Context Corruption Protection

Wraps LangGraph node execution to protect against context corruption.

Usage:
    from mycelium.adapters.langgraph import LangGraphIntegration
    from langgraph.graph import StateGraph

    graph = StateGraph(...)
    integration = LangGraphIntegration(graph)
    integration.register_tools({"fetch_user": fetch_user_func})
"""

import asyncio
from typing import Any, Callable, Optional, Dict
from functools import wraps
from mycelium.protections import tool, ContextSegmentation
from mycelium.core.runtime_context_corruption import (
    AgentRuntimeWithContextProtection,
    InvalidationPolicy,
)


class LangGraphContextProtection:
    """
    Adds AF-006 context protection to LangGraph agents.

    Works by:
    1. Intercepting tool calls in the agent
    2. Enforcing context cache rules
    3. Validating state freshness
    4. Providing audit trails
    """

    def __init__(
        self,
        policy: Optional[InvalidationPolicy] = None,
        verbose: bool = False,
    ):
        if policy is None:
            policy = InvalidationPolicy(
                default_ttl_steps=5,
                criticality_recheck_threshold=2,
                segmentation=ContextSegmentation.BOTH,
            )

        self.policy = policy
        self.runtime = AgentRuntimeWithContextProtection(policy=policy, verbose=verbose)
        self.step_counter = 0

    def register_tool(
        self,
        name: str,
        func: Callable,
        critical: bool = False,
        invalidate_after_steps: int = 5,
        entity_param: Optional[str] = None,
    ) -> None:
        """Register a tool with context protection rules."""
        decorated = tool(
            critical=critical,
            invalidate_after_steps=invalidate_after_steps,
            entity_param=entity_param,
        )(func)

        self.runtime.register_tools([decorated])

    async def call_tool(self, name: str, func: Callable, **kwargs) -> Any:
        """Call a tool through the protected runtime."""
        return await self.runtime.call_tool(name, func, **kwargs)

    def advance_step(self) -> None:
        """Advance step counter after agent step."""
        self.runtime.advance_step()
        self.step_counter += 1

    def get_cache_snapshot(self) -> Dict[str, Any]:
        """Get current cache state."""
        return self.runtime.get_cache_snapshot()

    def get_audit_log(self) -> list:
        """Get complete audit trail."""
        return self.runtime.get_audit_log()


def wrap_langgraph_node(
    node_func: Callable,
    protection: LangGraphContextProtection,
    tools_to_protect: Optional[Dict[str, Callable]] = None,
) -> Callable:
    """Wrap a LangGraph node function to add context protection."""
    if tools_to_protect:
        for name, func in tools_to_protect.items():
            protection.register_tool(name, func)

    @wraps(node_func)
    async def wrapped_node(state: Dict[str, Any]) -> Dict[str, Any]:
        if asyncio.iscoroutinefunction(node_func):
            result = await node_func(state)
        else:
            result = node_func(state)

        protection.advance_step()
        return result

    return wrapped_node


class LangGraphIntegration:
    """High-level integration for LangGraph agents."""

    def __init__(
        self,
        graph: Any = None,
        policy: Optional[InvalidationPolicy] = None,
        verbose: bool = False,
    ):
        """Initialize LangGraph integration."""
        self.graph = graph
        self.protection = LangGraphContextProtection(policy=policy, verbose=verbose)

    def register_tools(
        self, tools: Dict[str, Callable], critical_tools: Optional[list] = None
    ) -> None:
        """Register all tools with protection."""
        critical_tools = critical_tools or []
        for name, func in tools.items():
            self.protection.register_tool(
                name, func, critical=(name in critical_tools)
            )

    def get_protection(self) -> LangGraphContextProtection:
        """Get the underlying protection instance."""
        return self.protection

    def get_stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        snapshot = self.protection.get_cache_snapshot()
        audit = self.protection.get_audit_log()

        hits = len([e for e in audit if e["event_type"] == "get_hit"])
        misses = len([e for e in audit if "get_" in e["event_type"] and e["event_type"] != "get_hit"])

        return {
            "cache_entries": len(snapshot),
            "cache_hits": hits,
            "cache_misses": misses,
            "hit_rate": hits / (hits + misses) if (hits + misses) > 0 else 0,
            "steps": self.protection.step_counter,
        }
