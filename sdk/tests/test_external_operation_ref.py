"""Tests for external_operation_ref recording (v1.6.0, Phase 1).

Covers the durable field, the ``record_external_operation()`` marker, the
low-level ``attach_external_operation_ref()``, round-trip serialization, and
surfacing the ref in the hard-block message for later reconciliation.
"""

from __future__ import annotations

import pytest

from mycelium import (
    ActionLedger,
    InMemoryLedgerStorage,
    LedgerEntry,
    LedgerHardBlockError,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    execution_scope,
    ledger_sync,
    record_external_operation,
    side_effect,
)
from mycelium.action_ledger import get_active_transition


def _binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
    )


def test_entry_round_trips_external_operation_ref() -> None:
    entry = LedgerEntry(
        request_id="r1",
        tool="charge",
        args=[],
        kwargs={},
        status="completed",
        terminal_outcome=TerminalOutcome.COMPLETED.value,
        external_operation_ref="pi_123",
    )
    restored = LedgerEntry.from_dict(entry.to_dict())
    assert restored.external_operation_ref == "pi_123"


def test_entry_defaults_ref_to_none_for_legacy_records() -> None:
    legacy = {
        "request_id": "r1",
        "tool": "charge",
        "args": [],
        "kwargs": {},
        "status": "completed",
        "terminal_outcome": TerminalOutcome.COMPLETED.value,
    }
    entry = LedgerEntry.from_dict(legacy)
    assert entry.external_operation_ref is None


def test_attach_external_operation_ref_low_level() -> None:
    storage = InMemoryLedgerStorage()
    action_ledger = ActionLedger(storage=storage)
    rid = "rid-attach"
    action_ledger.claim_side_effecting(rid, "charge", (), {}, _binding())

    action_ledger.attach_external_operation_ref(rid, "pi_abc")
    assert storage.get(rid).external_operation_ref == "pi_abc"


def test_record_external_operation_sets_ref_during_run() -> None:
    storage = InMemoryLedgerStorage()
    rids: list[str] = []

    @ledger_sync(storage=storage, transition_binding=_binding())
    def charge(amount: float) -> dict[str, str]:
        active = get_active_transition()
        assert active is not None
        rids.append(active.request_id)
        with side_effect():
            record_external_operation("pi_live_1")
        return {"ok": True}

    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        charge(amount=10.0, tool_call_id="c1")

    assert storage.get(rids[0]).external_operation_ref == "pi_live_1"


def test_ref_retained_on_ambiguous_failure_and_surfaced_in_hard_block() -> None:
    storage = InMemoryLedgerStorage()
    rids: list[str] = []

    @ledger_sync(storage=storage, transition_binding=_binding())
    def charge(amount: float) -> dict[str, str]:
        active = get_active_transition()
        assert active is not None
        rids.append(active.request_id)
        with side_effect():
            record_external_operation("pi_live_2")
            raise RuntimeError("provider timeout after intent created")

    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        with pytest.raises(RuntimeError):
            charge(amount=10.0, tool_call_id="c1")

        entry = storage.get(rids[0])
        assert entry.resolved_terminal_outcome() == TerminalOutcome.UNKNOWN
        assert entry.side_effect_boundary == SideEffectBoundary.MAYBE_CROSSED.value
        assert entry.external_operation_ref == "pi_live_2"

        with pytest.raises(LedgerHardBlockError, match="pi_live_2"):
            charge(amount=10.0, tool_call_id="c1")


def test_record_external_operation_outside_tool_warns() -> None:
    with pytest.warns(UserWarning, match="outside a ledgered tool"):
        record_external_operation("pi_orphan")
