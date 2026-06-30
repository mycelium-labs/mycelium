"""Interactive demo: LangGraph duplicate tool call on retry (langgraph#7417)."""

from __future__ import annotations

from mycelium import ledger_sync

ISSUE_URL = "https://github.com/langchain-ai/langgraph/issues/7417"


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def run_demo() -> None:
    """Show the bug without Mycelium, then the fix with @ledger_sync."""
    print("Mycelium quickstart demo")
    print(f"Bug: LangGraph redispatches a long tool call while the first is still running")
    print(f"Source: {ISSUE_URL}")

    executions: list[str] = []

    def run_slow_subagent(task: str) -> dict[str, str]:
        executions.append(task)
        print(f"  [EXECUTING] subagent_task({task!r})")
        return {"task": task, "result": "done"}

    _section("Without Mycelium")
    executions.clear()
    run_slow_subagent("analyze_market")
    run_slow_subagent("analyze_market")
    print(f"Executions: {len(executions)}  ← duplicate side effect + cost")

    _section("With Mycelium (5 lines)")
    executions.clear()

    @ledger_sync()
    def subagent_task(task: str, duration_seconds: int = 0) -> dict[str, str]:
        return run_slow_subagent(task)

    tool_call_id = "call_subagent_1"
    subagent_task(
        task="analyze_market",
        duration_seconds=300,
        tool_call_id=tool_call_id,
    )
    subagent_task(
        task="analyze_market",
        duration_seconds=300,
        tool_call_id=tool_call_id,
    )
    print(f"Executions: {len(executions)}  ← redispatch returned cached result")

    _section("Install")
    print("pip install mycelium-runtime")
    print("mycelium init")
    print()
    print("from mycelium import ledger_sync")
    print()
    print("@ledger_sync()")
    print("def subagent_task(task: str) -> dict:")
    print("    return run_slow_subagent(task)")
    print()
    print("# LangGraph passes tool_call_id — same id won't execute twice")
