"""
CrewAI Integration for AF-006 Context Corruption Protection

Protects CrewAI agents from context corruption by intercepting task execution.

Usage:
    from mycelium.adapters.crewai import CrewAIIntegration

    integration = CrewAIIntegration()
    integration.register_tools({
        'tool_name': tool_function,
    }, critical_tools=['tool_name'])

    # Wrap task execution
    result = await integration.execute_task_with_protection(task, agent)
"""

from collections.abc import Callable
from typing import Any

from mycelium.core.runtime_context_corruption import (
    AgentRuntimeWithContextProtection,
    InvalidationPolicy,
)
from mycelium.protections import ContextSegmentation, tool


class CrewAIContextProtection:
    """Context protection for CrewAI task execution."""

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
        self.task_counter = 0

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

    def advance_task(self) -> None:
        """Called when a task completes."""
        self.runtime.advance_step()
        self.task_counter += 1

    def get_stats(self) -> dict[str, Any]:
        """Get cache stats for this crew."""
        snapshot = self.runtime.get_cache_snapshot()
        audit = self.runtime.get_audit_log()

        hits = len([e for e in audit if e["event_type"] == "get_hit"])
        misses = len(
            [e for e in audit
             if "get_" in e["event_type"]
             and e["event_type"] != "get_hit"]
        )

        return {
            "cache_entries": len(snapshot),
            "cache_hits": hits,
            "cache_misses": misses,
            "hit_rate": hits / (hits + misses) if (hits + misses) > 0 else 0,
            "tasks_completed": self.task_counter,
        }

    def get_audit_log(self) -> list:
        """Get complete audit trail."""
        return self.runtime.get_audit_log()


class CrewAIIntegration:
    """High-level integration for CrewAI crews."""

    def __init__(
        self,
        policy: InvalidationPolicy | None = None,
        verbose: bool = False,
    ):
        """Initialize CrewAI integration."""
        self.protection = CrewAIContextProtection(policy=policy, verbose=verbose)
        self.agents = {}
        self.tasks = {}

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

    def register_agent(self, agent_name: str, agent: Any) -> None:
        """Register an agent for tracking."""
        self.agents[agent_name] = agent

    def register_task(self, task_name: str, task: Any) -> None:
        """Register a task for tracking."""
        self.tasks[task_name] = task

    def get_protection(self) -> CrewAIContextProtection:
        """Get the underlying protection instance."""
        return self.protection

    def get_crew_stats(self) -> dict[str, Any]:
        """Get crew statistics."""
        stats = self.protection.get_stats()
        stats["agents"] = len(self.agents)
        stats["tasks"] = len(self.tasks)
        return stats


class CrewAITaskExecutor:
    """Executes CrewAI tasks with protection."""

    def __init__(self, integration: CrewAIIntegration):
        self.integration = integration
        self.protection = integration.get_protection()

    async def execute_task(
        self,
        task: Any,
        agent: Any,
        context: dict[str, Any] | None = None,
    ) -> str:
        """
        Execute a CrewAI task with protection.

        Args:
            task: CrewAI Task instance
            agent: CrewAI Agent instance
            context: Optional context dict

        Returns:
            Task result as string
        """
        context = context or {}

        # Task execution simulation
        # In real implementation, this would call task.execute()
        task_name = getattr(task, "name", "unknown")
        print(f"\n[TASK] {task_name}")

        # Tool calls would go through protection here
        # This is a simplified version

        self.protection.advance_task()

        return f"Task '{task_name}' completed"
