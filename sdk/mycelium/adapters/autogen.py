"""AutoGen Integration for AF-006 Context Corruption Protection"""

from collections.abc import Callable
from typing import Any

from mycelium.core.runtime_context_corruption import (
    AgentRuntimeWithContextProtection,
    InvalidationPolicy,
)
from mycelium.protections import ContextSegmentation, tool


class AutoGenContextProtection:
    """Context protection for AutoGen multi-agent systems."""

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
        self.message_counter = 0

    def register_tool(
        self,
        name: str,
        func: Callable,
        critical: bool = False,
        invalidate_after_steps: int = 5,
    ) -> None:
        """Register tool with AutoGen agent."""
        decorated = tool(
            critical=critical,
            invalidate_after_steps=invalidate_after_steps,
        )(func)
        self.runtime.register_tools([decorated])

    async def call_tool_protected(self, name: str, func: Callable, **kwargs) -> Any:
        """Call tool through protection."""
        return await self.runtime.call_tool(name, func, **kwargs)

    def handle_message(self) -> None:
        """Called when agent sends/receives message."""
        self.runtime.advance_step()
        self.message_counter += 1

    def get_stats(self) -> dict[str, Any]:
        """Get AutoGen conversation stats."""
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
            "messages_processed": self.message_counter,
        }

    def get_audit_log(self) -> list:
        """Get complete audit trail."""
        return self.runtime.get_audit_log()


class AutoGenIntegration:
    """High-level AutoGen integration."""

    def __init__(
        self,
        policy: InvalidationPolicy | None = None,
        verbose: bool = False,
    ):
        self.protection = AutoGenContextProtection(policy=policy, verbose=verbose)

    def register_tools(
        self,
        tools: dict[str, Callable],
        critical_tools: list | None = None,
    ) -> None:
        """Register tools for AutoGen agents."""
        critical_tools = critical_tools or []
        for name, func in tools.items():
            self.protection.register_tool(name, func, critical=(name in critical_tools))

    def get_protection(self) -> AutoGenContextProtection:
        """Get protection instance."""
        return self.protection
