"""Tests for bundled proof runners."""

from __future__ import annotations

from mycelium.proofs.langgraph_7417 import (
    load_fixture,
    prove_ledger_deduplication,
    reproduce_baseline_duplicate,
)


def test_load_fixture_has_langgraph_7417_metadata() -> None:
    fixture = load_fixture()
    assert fixture["id"] == "langgraph-7417-duplicate-execution"
    assert "langgraph/issues/7417" in fixture["source_url"]
    assert fixture["scenario"]["tool_call_id"] == "call_subagent_1"


def test_baseline_reproduces_duplicate_execution() -> None:
    executions = reproduce_baseline_duplicate()
    assert len(executions) == 2


def test_prove_ledger_deduplication() -> None:
    result = prove_ledger_deduplication()
    assert len(result["executions"]) == 1
    assert result["r1"] == result["r2"]
    assert result["side_effect_class"] == "subagent"
    assert result["agent_id"] == "my-agent"
