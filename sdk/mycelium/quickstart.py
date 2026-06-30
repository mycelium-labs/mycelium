"""Run the langgraph#7417 proof demo (bundled fixture + ledger guard)."""

from __future__ import annotations

import sys
from typing import Any

from mycelium.proofs.langgraph_7417 import (
    load_fixture,
    prove_ledger_deduplication,
    reproduce_baseline_duplicate,
)


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _print_execute(tool_name: str, record: dict[str, Any]) -> None:
    print(f"  [EXECUTING] {tool_name}({record!r})")


def _pass(msg: str) -> None:
    print(f"PASS: {msg}")


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)


def run_demo() -> int:
    """Run baseline + guarded proof from the bundled fixture. Returns exit code."""
    fixture = load_fixture()
    scenario = fixture["scenario"]

    print("Mycelium proof demo (real test)")
    print(f"Fixture: {fixture['id']}")
    print(f"Source:  {fixture['source_url']}")
    print(f"Pattern: {fixture['pattern']}")

    _section("[1/2] Baseline: unguarded redispatch (failure class)")
    print(
        f"Simulating redispatch of {scenario['tool_name']!r} "
        f"(runtime={scenario['runtime']!r}, no ActionLedger)"
    )
    baseline = reproduce_baseline_duplicate(fixture, on_execute=_print_execute)
    print(f"Executions: {len(baseline)}")
    if len(baseline) == 2:
        _pass("duplicate side effect reproduced (this is the bug)")
    else:
        _fail(f"expected 2 executions, got {len(baseline)}")
        return 1

    _section("[2/2] Guarded: ledger deduplication")
    print(
        f"Same scenario with @ledger_sync, tool_call_id={scenario['tool_call_id']!r}"
    )
    try:
        result = prove_ledger_deduplication(fixture, on_execute=_print_execute)
    except AssertionError as exc:
        _fail(str(exc))
        return 1

    print(f"Executions: {len(result['executions'])}")
    print(f"r1 == r2:   {result['r1'] == result['r2']}")
    _pass("redispatch returned cached result, side effect ran once")

    _section("Use in your agent")
    print("pip install mycelium-runtime")
    print("mycelium init")
    print()
    print("from mycelium import ledger_sync")
    print()
    print("@ledger_sync()")
    print(f"def {scenario['tool_name']}(task: str, duration_seconds: int) -> dict:")
    print("    return run_slow_subagent(task)")
    print()
    print("# Pass tool_call_id from LangGraph on each invocation")

    return 0
