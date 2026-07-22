"""Run the langgraph#7417 proof demo (bundled fixture + transition envelope)."""

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


def run_demo(*, redis: bool = False) -> int:
    """Run baseline + guarded proof from the bundled fixture. Returns exit code.

    When ``redis=True``, also runs the two-worker real-Redis Cloud-style proof
    (requires a reachable Redis; see ``MYCELIUM_TEST_REDIS_URL``).
    """
    fixture = load_fixture()
    scenario = fixture["scenario"]

    print("Mycelium proof demo (real test)")
    print("Pitch: transition envelope (class + lease + terminal + hard-block),")
    print("       not only idempotency key + cached result.")
    print(
        "LangGraph Cloud may redispatch long tools ~180s "
        "(BG_JOB_HEARTBEAT sweep)."
    )
    print(f"Fixture: {fixture['id']}")
    print(f"Source:  {fixture['source_url']}")
    print(f"Pattern: {fixture['pattern']}")

    _section("[1/2] Baseline: unguarded redispatch (failure class)")
    print(
        f"Simulating redispatch of {scenario['tool_name']!r} "
        f"(runtime={scenario['runtime']!r}, no transition envelope)"
    )
    baseline = reproduce_baseline_duplicate(fixture, on_execute=_print_execute)
    print(f"Executions: {len(baseline)}")
    if len(baseline) == 2:
        _pass("duplicate side effect reproduced (this is the bug)")
    else:
        _fail(f"expected 2 executions, got {len(baseline)}")
        return 1

    _section("[2/2] Guarded: transition envelope (v1.3)")
    print(
        f"Same scenario with transition + side_effect_class=non_idempotent_mutate, "
        f"tool_call_id={scenario['tool_call_id']!r}"
    )
    try:
        result = prove_ledger_deduplication(fixture, on_execute=_print_execute)
    except AssertionError as exc:
        _fail(str(exc))
        return 1

    print(f"Executions: {len(result['executions'])}")
    print(f"r1 == r2:   {result['r1'] == result['r2']}")
    print(f"side_effect_class: {result['side_effect_class']}")
    _pass("redispatch resolved existing transition, side effect ran once")

    if redis:
        from mycelium.proofs.langgraph_7417_redis import (
            ENV_REDIS_URL,
            prove_two_worker_redis_redispatch,
            redis_reachable,
            resolve_redis_url,
        )

        _section("[3/3] Cloud-style: 2 workers + real Redis")
        url = resolve_redis_url()
        print(f"Redis URL: {url} (override with {ENV_REDIS_URL})")
        if not redis_reachable(url):
            _fail(f"Redis not reachable at {url!r}")
            return 1
        try:
            multi = prove_two_worker_redis_redispatch(url=url)
        except (AssertionError, RuntimeError) as exc:
            _fail(str(exc))
            return 1
        print(f"Workers:     {multi['workers']}")
        print(f"Executions:  {multi['executions']}")
        print(f"request_id:  {multi['request_id']}")
        _pass("second worker polled; side effect ran once on shared Redis ledger")

    _section("Use in your agent")
    print("pip install mycelium-runtime")
    print("pip install 'mycelium-runtime[redis]'  # multi-worker / Cloud-style")
    print("mycelium init")
    print("mycelium demo --redis               # optional 2-worker Redis proof")
    print()
    print("from mycelium import load_config")
    print()
    print('config = load_config("mycelium.yaml")')
    print()
    print("@config.apply")
    print(f"def {scenario['tool_name']}(task: str, duration_seconds: int) -> dict:")
    print("    return run_slow_subagent(task)")
    print()
    print("# Pass tool_call_id from LangGraph on each invocation")
    return 0
