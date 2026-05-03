"""
Smolagents Integration for AF-006 Context Corruption Protection

Provides context protection for Smolagents framework with tool safety.

Usage:
    from mycelium.adapters.smolagents import SmolagentsIntegration

    integration = SmolagentsIntegration()
    integration.register_tools({
        'tool_name': tool_function,
    }, critical_tools=['tool_name'])

    protection = integration.get_protection()
    result = await protection.call_tool_protected(...)
"""

from collections.abc import Callable
from typing import Any

from mycelium.core.runtime_context_corruption import (
    AgentRuntimeWithContextProtection,
    InvalidationPolicy,
)
from mycelium.protections import ContextSegmentation, tool


class SmolagentsContextProtection:
    """Context protection for Smolagents tool execution."""

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
        self.runtime = AgentRuntimeWithContextProtection(policy=policy, verbose=verbose)
        self.action_counter = 0

    def register_tool(
        self,
        name: str,
        func: Callable,
        critical: bool = False,
        invalidate_after_steps: int = 5,
        entity_param: str | None = None,
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

    def advance_action(self) -> None:
        """Called after each agent action."""
        self.runtime.advance_step()
        self.action_counter += 1

    def get_stats(self) -> dict[str, Any]:
        """Get cache stats for this agent."""
        snapshot = self.runtime.get_cache_snapshot()
        audit = self.runtime.get_audit_log()

        hits = len([e for e in audit if e["event_type"] == "get_hit"])
        misses = len(
            [e for e in audit if "get_" in e["event_type"] and e["event_type"] != "get_hit"]
        )

        return {
            "cache_entries": len(snapshot),
            "cache_hits": hits,
            "cache_misses": misses,
            "hit_rate": hits / (hits + misses) if (hits + misses) > 0 else 0,
            "actions": self.action_counter,
        }

    def get_audit_log(self) -> list:
        """Get complete audit trail."""
        return self.runtime.get_audit_log()

    def get_cache_snapshot(self) -> dict[str, Any]:
        """Get current cache state."""
        return self.runtime.get_cache_snapshot()


class SmolagentsIntegration:
    """High-level integration for Smolagents framework."""

    def __init__(
        self,
        policy: InvalidationPolicy | None = None,
        verbose: bool = False,
    ):
        """Initialize Smolagents integration."""
        self.protection = SmolagentsContextProtection(policy=policy, verbose=verbose)

    def register_tools(
        self,
        tools: dict[str, Callable],
        critical_tools: list[str] | None = None,
    ) -> None:
        """Register all tools with protection."""
        critical_tools = critical_tools or []
        for name, func in tools.items():
            self.protection.register_tool(
                name, func, critical=(name in critical_tools)
            )

    def get_protection(self) -> SmolagentsContextProtection:
        """Get the underlying protection instance."""
        return self.protection

    def get_stats(self) -> dict[str, Any]:
        """Get agent statistics."""
        return self.protection.get_stats()
