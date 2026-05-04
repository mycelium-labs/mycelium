"""OpenHands Integration for AF-006 Context Corruption Protection"""

from collections.abc import Callable
from typing import Any

from mycelium.core.runtime_context_corruption import (
    AgentRuntimeWithContextProtection,
    InvalidationPolicy,
)
from mycelium.protections import ContextSegmentation, tool


class OpenHandsContextProtection:
    """Context protection for OpenHands agent execution."""

    def __init__(self, policy: InvalidationPolicy | None = None, verbose: bool = False):
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
        func: Callable[..., Any],
        critical: bool = False,
        invalidate_after_steps: int = 5,
        entity_param: str | None = None,
    ) -> None:
        decorated = tool(
            critical=critical,
            invalidate_after_steps=invalidate_after_steps,
            entity_param=entity_param,
        )(func)
        self.runtime.register_tools([decorated])

    async def call_tool_protected(self, name: str, func: Callable[..., Any], **kwargs: Any) -> Any:
        return await self.runtime.call_tool(name, func, **kwargs)

    def advance_action(self) -> None:
        """Call after each OpenHands agent action."""
        self.runtime.advance_step()
        self.action_counter += 1

    def get_audit_log(self) -> list[dict[str, Any]]:
        return self.runtime.get_audit_log()

    def get_stats(self) -> dict[str, Any]:
        audit = self.runtime.get_audit_log()
        hits = sum(1 for e in audit if e["event_type"] == "get_hit")
        misses = sum(1 for e in audit if "get_" in e["event_type"] and e["event_type"] != "get_hit")
        return {
            "cache_hits": hits,
            "cache_misses": misses,
            "hit_rate": hits / (hits + misses) if (hits + misses) > 0 else 0,
            "actions": self.action_counter,
        }


class OpenHandsIntegration:
    """High-level integration for OpenHands agents."""

    def __init__(self, policy: InvalidationPolicy | None = None, verbose: bool = False):
        self.protection = OpenHandsContextProtection(policy=policy, verbose=verbose)

    def register_tools(
        self,
        tools: dict[str, Callable[..., Any]],
        critical_tools: list[str] | None = None,
    ) -> None:
        critical_tools = critical_tools or []
        for name, func in tools.items():
            self.protection.register_tool(name, func, critical=(name in critical_tools))

    def get_protection(self) -> OpenHandsContextProtection:
        return self.protection
