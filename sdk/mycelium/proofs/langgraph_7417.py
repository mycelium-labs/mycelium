"""Proof: langgraph#7417 duplicate tool execution on redispatch.

Shared by ``mycelium demo``, ``proof/test_proof_af002.py``, and unit tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib import resources
from typing import Any

from mycelium import load_config_from_string

FIXTURE_NAME = "langgraph-7417-duplicate-tool-execution.json"
ExecuteHook = Callable[[str, dict[str, Any]], None]

# Same shape as ``mycelium init`` quickstart (memory storage for the demo).
TRANSITION_DEMO_YAML = """\
transition:
  agent_id: my-agent
  policy_version: "2026.07.1"

action_ledger:
  storage: memory
  tools:
    - subagent_task

tools:
  subagent_task:
    side_effect_class: non_idempotent_mutate
"""


def load_fixture() -> dict[str, Any]:
    path = resources.files("mycelium.fixtures") / FIXTURE_NAME
    return json.loads(path.read_text(encoding="utf-8"))


def _scenario(fixture: dict[str, Any]) -> dict[str, Any]:
    return fixture["scenario"]


def reproduce_baseline_duplicate(
    fixture: dict[str, Any] | None = None,
    *,
    on_execute: ExecuteHook | None = None,
) -> list[dict[str, Any]]:
    """Unguarded tool: redispatch runs the side effect again (failure class)."""
    fixture = fixture or load_fixture()
    scenario = _scenario(fixture)
    args = dict(scenario["args"])
    executions: list[dict[str, Any]] = []

    def subagent_task(task: str, duration_seconds: int) -> dict[str, Any]:
        record = {"task": task, "duration_seconds": duration_seconds}
        executions.append(record)
        if on_execute is not None:
            on_execute(scenario["tool_name"], record)
        return {"task": task, "result": "done"}

    # LangGraph Cloud redispatches while the original is still in flight.
    subagent_task(**args)
    subagent_task(**args)
    return executions


def prove_ledger_deduplication(
    fixture: dict[str, Any] | None = None,
    *,
    on_execute: ExecuteHook | None = None,
) -> dict[str, Any]:
    """Real proof: same tool_call_id redispatched → executes only once.

    Uses the v1.3 transition envelope (``transition:`` + ``side_effect_class``),
    matching what ``mycelium init`` scaffolds.

    Raises ``AssertionError`` if the guard fails (same assertions as proof/ test).
    """
    fixture = fixture or load_fixture()
    scenario = _scenario(fixture)
    args = dict(scenario["args"])
    tool_call_id = scenario["tool_call_id"]
    executions: list[dict[str, Any]] = []

    config = load_config_from_string(TRANSITION_DEMO_YAML)

    @config.apply
    def subagent_task(task: str, duration_seconds: int) -> dict[str, Any]:
        record = {"task": task, "duration_seconds": duration_seconds}
        executions.append(record)
        if on_execute is not None:
            on_execute(scenario["tool_name"], record)
        return {"task": task, "result": "done"}

    r1 = subagent_task(**args, tool_call_id=tool_call_id)
    r2 = subagent_task(**args, tool_call_id=tool_call_id)

    expected = {"task": args["task"], "result": "done"}
    assert len(executions) == 1, f"expected 1 execution, got {len(executions)}"
    assert r1 == r2 == expected, f"results mismatch: r1={r1!r} r2={r2!r}"

    binding = config.tool_transition_binding(config.tools["subagent_task"])
    assert binding is not None

    return {
        "executions": executions,
        "r1": r1,
        "r2": r2,
        "tool_call_id": tool_call_id,
        "fixture": fixture,
        "side_effect_class": binding.side_effect_class.value,
        "agent_id": binding.agent_id,
    }
