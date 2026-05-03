"""
OpenAI Agents SDK Integration for AF-006 Context Corruption Protection

Provides context protection for OpenAI Agents SDK with tool execution safety.

Usage:
    from mycelium.adapters.openai_agents import OpenAIAgentsIntegration

    integration = OpenAIAgentsIntegration()
    integration.register_tools({
        'tool_name': tool_function,
    }, critical_tools=['tool_name'])

    protection = integration.get_protection()
    result = await protection.call_tool_protected(...)
"""

import asyncio
from typing import Any, Callable, Optional, Dict, List
from mycelium.protections import tool, ContextSegmentation
from mycelium.core.runtime_context_corruption import (
    AgentRuntimeWithContextProtection,
    InvalidationPolicy,
)


class OpenAIAgentsContextProtection:
    """Context protection for OpenAI Agents SDK tool execution."""

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
        self.call_counter = 0

    def register_tool(
        self,
        name: str,
        func: Callable,
        critical: bool = False,
        invalidate_after_steps: int = 5,
        entity_param: Optional[str] = None,
    ) -> None:
        """Register a tool for protection."""
        decorated = tool(
            critical=critical,
            invalidate_after_steps=invalidate_after_steps,
            entity_param=entity_param,
        )(func)

        self.runtime.register_tools([decorated])

    async def call_tool_protected(
        self, name: str, func: Callable, **kwargs
    ) -> Any:
        """Call a tool through protection."""
        return await self.runtime.call_tool(name, func, **kwargs)

    def advance_step(self) -> None:
        """Called after each agent step."""
        self.runtime.advance_step()
        self.call_counter += 1

    def get_stats(self) -> Dict[str, Any]:
        """Get cache stats for this agent."""
        snapshot = self.runtime.get_cache_snapshot()
        audit = self.runtime.get_audit_log()

        hits = len([e for e in audit if e["event_type"] == "get_hit"])
        misses = len([e for e in audit if "get_" in e["event_type"] and e["event_type"] != "get_hit"])

        return {
            "cache_entries": len(snapshot),
            "cache_hits": hits,
            "cache_misses": misses,
            "hit_rate": hits / (hits + misses) if (hits + misses) > 0 else 0,
            "steps": self.call_counter,
        }

    def get_audit_log(self) -> list:
        """Get complete audit trail."""
        return self.runtime.get_audit_log()

    def get_cache_snapshot(self) -> Dict[str, Any]:
        """Get current cache state."""
        return self.runtime.get_cache_snapshot()


class OpenAIAgentsIntegration:
    """High-level integration for OpenAI Agents SDK."""

    def __init__(
        self,
        policy: Optional[InvalidationPolicy] = None,
        verbose: bool = False,
    ):
        """Initialize OpenAI Agents integration."""
        self.protection = OpenAIAgentsContextProtection(policy=policy, verbose=verbose)

    def register_tools(
        self,
        tools: Dict[str, Callable],
        critical_tools: Optional[List[str]] = None,
    ) -> None:
        """Register all tools with protection."""
        critical_tools = critical_tools or []
        for name, func in tools.items():
            self.protection.register_tool(
                name, func, critical=(name in critical_tools)
            )

    def get_protection(self) -> OpenAIAgentsContextProtection:
        """Get the underlying protection instance."""
        return self.protection

    def get_stats(self) -> Dict[str, Any]:
        """Get agent statistics."""
        return self.protection.get_stats()
