"""REPAIR gate: heal incomplete durable transition context before execute."""

from __future__ import annotations

import time
from dataclasses import replace

from mycelium import (
    ActionLedger,
    InMemoryLedgerStorage,
    LedgerEntry,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionGate,
    TransitionScope,
    execution_scope,
    ledger_sync,
    repair_transition_fields,
    transition_needs_repair,
)
from mycelium.transition_resolution import (
    resolve_read_only_gate,
    resolve_side_effect_gate,
)


def _payment_binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="a",
        policy_version="1",
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
    )


def _incomplete_inflight(*, request_id: str = "pay-1") -> LedgerEntry:
    return LedgerEntry(
        request_id=request_id,
        tool="send_payment",
        args=[],
        kwargs={"amount": 10},
        status="in-flight",
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        lease_until=time.time() + 3600,
        idempotency_key="",
        side_effect_boundary="",
    )


def test_transition_needs_repair_detects_missing_key_and_boundary() -> None:
    entry = _incomplete_inflight()
    assert transition_needs_repair(entry) is True
    updates = repair_transition_fields(entry)
    assert updates["idempotency_key"] == "pay-1"
    assert updates["side_effect_boundary"] == SideEffectBoundary.NOT_CROSSED.value


def test_complete_entry_does_not_need_repair() -> None:
    entry = LedgerEntry(
        request_id="pay-2",
        tool="send_payment",
        args=[],
        kwargs={},
        status="in-flight",
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        lease_until=time.time() + 60,
        idempotency_key="pay-2",
        side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
    )
    assert transition_needs_repair(entry) is False
    assert resolve_side_effect_gate(entry, _payment_binding()) == TransitionGate.POLL


def test_resolve_side_effect_gate_returns_repair_before_poll() -> None:
    entry = _incomplete_inflight()
    assert resolve_side_effect_gate(entry, _payment_binding()) == TransitionGate.REPAIR


def test_resolve_read_only_gate_returns_repair() -> None:
    entry = LedgerEntry(
        request_id="read-1",
        tool="fetch",
        args=[],
        kwargs={},
        status="in-flight",
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        lease_until=time.time() + 60,
        idempotency_key="",
    )
    assert resolve_read_only_gate(entry) == TransitionGate.REPAIR


def test_status_terminal_drift_needs_repair() -> None:
    entry = LedgerEntry(
        request_id="pay-3",
        tool="send_payment",
        args=[],
        kwargs={},
        status="completed",
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        result={"ok": True},
        idempotency_key="pay-3",
        side_effect_boundary=SideEffectBoundary.CROSSED.value,
        finished_at=time.time(),
    )
    assert transition_needs_repair(entry) is True
    updates = repair_transition_fields(entry)
    assert updates["terminal_outcome"] == TerminalOutcome.COMPLETED.value
    healed = replace(entry, **updates)
    assert transition_needs_repair(healed) is False
    assert resolve_side_effect_gate(healed, _payment_binding()) == TransitionGate.RETURN


def test_invalid_terminal_inferred_from_status() -> None:
    entry = LedgerEntry(
        request_id="pay-4",
        tool="send_payment",
        args=[],
        kwargs={},
        status="failed",
        terminal_outcome="not-a-real-outcome",
        idempotency_key="pay-4",
        side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
        error="boom",
        finished_at=time.time(),
    )
    assert transition_needs_repair(entry) is True
    updates = repair_transition_fields(entry)
    assert updates["terminal_outcome"] == TerminalOutcome.FAILED_BEFORE_EFFECT.value


def test_ledger_repair_then_poll_on_claim() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage, lease_ttl=3600.0)
    incomplete = _incomplete_inflight(request_id="pay-5")
    storage.set(incomplete)

    repaired = ledger.repair_transition("pay-5")
    assert repaired.idempotency_key == "pay-5"
    assert repaired.side_effect_boundary == SideEffectBoundary.NOT_CROSSED.value
    assert transition_needs_repair(repaired) is False
    assert resolve_side_effect_gate(repaired, _payment_binding()) == TransitionGate.POLL


def test_claim_side_effecting_repairs_then_returns_completed() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    storage.set(
        LedgerEntry(
            request_id="pay-6",
            tool="send_payment",
            args=[],
            kwargs={"amount": 5},
            status="completed",
            terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
            result={"charged": 5},
            idempotency_key="",
            side_effect_boundary=SideEffectBoundary.CROSSED.value,
            finished_at=time.time(),
            lease_until=None,
        )
    )
    entry = ledger.claim_side_effecting(
        "pay-6",
        "send_payment",
        (),
        {"amount": 5},
        _payment_binding(),
    )
    assert entry.result == {"charged": 5}
    assert entry.terminal_outcome == TerminalOutcome.COMPLETED.value
    assert entry.idempotency_key == "pay-6"


def test_decorator_redispatch_repairs_incomplete_completed() -> None:
    storage = InMemoryLedgerStorage()
    binding = _payment_binding()
    calls: list[float] = []

    @ledger_sync(storage=storage, transition_binding=binding)
    def send_payment(amount: float) -> dict:
        calls.append(amount)
        return {"charged": amount}

    with execution_scope(TransitionScope(thread_id="t", run_id="r", node="n")):
        first = send_payment(10.0)
        entries = storage.list_all()
        assert len(entries) == 1
        # Corrupt the durable record as if a partial write dropped fields.
        storage.set(
            replace(
                entries[0],
                idempotency_key="",
            )
        )
        second = send_payment(10.0)

    assert first == {"charged": 10.0}
    assert second == {"charged": 10.0}
    assert calls == [10.0]
    healed = storage.list_all()[0]
    assert healed.idempotency_key is not None
    assert healed.side_effect_boundary == SideEffectBoundary.CROSSED.value
