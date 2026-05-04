"""LiveKit Agents Integration for AF-006 Context Corruption Protection

Addresses the exact failure pattern from livekit/agents#5408:
- Stale activity signals causing generate_reply() timeouts
- STT transcripts discarded unconditionally
"""

from collections.abc import Callable
from typing import Any

from mycelium.core.runtime_context_corruption import (
    AgentRuntimeWithContextProtection,
    InvalidationPolicy,
)
from mycelium.protections import ContextSegmentation, tool


class LiveKitContextProtection:
    """
    Context protection for LiveKit agent tool execution.

    Designed around issue #5408:
    - Activity signals get TTL=1 step so they can't be resent stale
    - Audio and text contexts are entity-segmented so they don't mix
    """

    def __init__(self, policy: InvalidationPolicy | None = None, verbose: bool = False):
        if policy is None:
            policy = InvalidationPolicy(
                default_ttl_steps=3,
                criticality_recheck_threshold=1,
                segmentation=ContextSegmentation.BOTH,
            )
        self.policy = policy
        self.runtime = AgentRuntimeWithContextProtection(policy=policy, verbose=verbose)
        self.turn_counter = 0

    def register_tool(
        self,
        name: str,
        func: Callable[..., Any],
        critical: bool = False,
        invalidate_after_steps: int = 3,
        entity_param: str | None = None,
    ) -> None:
        decorated = tool(
            critical=critical,
            invalidate_after_steps=invalidate_after_steps,
            entity_param=entity_param,
        )(func)
        self.runtime.register_tools([decorated])

    async def call_tool_protected(self, name: str, func: Callable[..., Any], **kwargs: Any) -> Any:
        """Wrap a LiveKit tool call with AF-006 protection."""
        return await self.runtime.call_tool(name, func, **kwargs)

    def advance_turn(self) -> None:
        """Call after each conversation turn to age TTLs."""
        self.runtime.advance_step()
        self.turn_counter += 1

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
            "turns": self.turn_counter,
        }


class LiveKitIntegration:
    """High-level integration for LiveKit agents."""

    def __init__(self, policy: InvalidationPolicy | None = None, verbose: bool = False):
        self.protection = LiveKitContextProtection(policy=policy, verbose=verbose)

    def register_tools(
        self,
        tools: dict[str, Callable[..., Any]],
        critical_tools: list[str] | None = None,
    ) -> None:
        critical_tools = critical_tools or []
        for name, func in tools.items():
            self.protection.register_tool(name, func, critical=(name in critical_tools))

    def get_protection(self) -> LiveKitContextProtection:
        return self.protection
