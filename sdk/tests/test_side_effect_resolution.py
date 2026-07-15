"""Tests for side-effecting transition resolution (payment-class rules)."""

from __future__ import annotations

import threading
import time

import pytest

from mycelium import (
    InMemoryLedgerStorage,
    LedgerEntry,
    LedgerHardBlockError,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    derive_transition_key_for_call,
    execution_scope,
    ledger_sync,
)
from mycelium.transition_resolution import TransitionGate, resolve_side_effect_gate


def _payment_binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.PAYMENT,
    )


def _idempotent_binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.IDEMPOTENT_WRITE,
    )


def test_payment_polls_in_flight_instead_of_pending_error() -> None:
    storage = InMemoryLedgerStorage()
    binding = _payment_binding()
    executions: list[int] = []
    started = threading.Event()

    @ledger_sync(storage=storage, transition_binding=binding)
    def send_payment(amount: float) -> dict[str, str]:
        started.set()
        time.sleep(0.05)
        executions.append(1)
        return {"status": "sent"}

    results: list[dict[str, str]] = []

    def worker() -> None:
        with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
            results.append(
                send_payment(amount=10.0, tool_call_id="call_pay")
            )

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    assert started.wait(timeout=1.0)
    second.start()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert len(executions) == 1
    assert results[0] == results[1] == {"status": "sent"}


def test_payment_hard_blocks_expired_lease() -> None:
    """Ledger API: expired in-flight payment must hard-block (crewAI#5802 class)."""
    from mycelium import ActionLedger

    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    request_id = "expired-payment"

    storage.set(
        LedgerEntry(
            request_id=request_id,
            tool="send_payment",
            args=[],
            kwargs={"amount": 10.0},
            status="in-flight",
            terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
            lease_until=time.time() - 1,
            idempotency_key=request_id,
        )
    )

    with pytest.raises(LedgerHardBlockError, match="manual reconciliation"):
        ledger.claim_side_effecting(
            request_id,
            "send_payment",
            (),
            {"amount": 10.0},
            ToolTransitionBinding.for_tool(
                agent_id="demo",
                policy_version="1",
                side_effect_class=SideEffectClass.PAYMENT,
            ),
        )

    entry = storage.get(request_id)
    assert entry is not None
    assert entry.terminal_outcome == TerminalOutcome.BLOCKED.value


def test_crash_after_claim_before_complete_hard_blocks_redispatch() -> None:
    """crewAI#5802: claim succeeds, worker dies before complete, redispatch must not run.

    Simulates an expired in-flight transition, then retries through ``@ledger_sync``.
    The tool body must not execute again.
    """
    storage = InMemoryLedgerStorage()
    binding = _payment_binding()
    executions: list[str] = []

    args: tuple[()] = ()
    kwargs = {"amount": 42.0, "tool_call_id": "call_pay_5802"}

    with execution_scope(TransitionScope(thread_id="thread-1", run_id="run-1")):
        request_id = derive_transition_key_for_call(
            "send_payment", args, kwargs, binding
        )

        # Claim succeeded; process died before complete()/fail().
        storage.set(
            LedgerEntry(
                request_id=request_id,
                tool="send_payment",
                args=list(args),
                kwargs={"amount": 42.0},
                status="in-flight",
                terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
                side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
                lease_until=time.time() - 1.0,
                idempotency_key=request_id,
                owner="dead-worker:1",
            )
        )

        @ledger_sync(storage=storage, transition_binding=binding)
        def send_payment(amount: float) -> dict[str, str]:
            executions.append("executed")
            return {"status": "sent", "amount": str(amount)}

        with pytest.raises(LedgerHardBlockError, match="manual reconciliation"):
            send_payment(amount=42.0, tool_call_id="call_pay_5802")

    assert executions == []

    entry = storage.get(request_id)
    assert entry is not None
    assert entry.terminal_outcome == TerminalOutcome.BLOCKED.value


def test_payment_hard_blocks_failed_after_effect_retry() -> None:
    from mycelium import ActionLedger

    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    request_id = "failed-payment"

    storage.set(
        LedgerEntry(
            request_id=request_id,
            tool="send_payment",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.FAILED_AFTER_EFFECT.value,
            error="RuntimeError: charged",
            idempotency_key=request_id,
        )
    )

    with pytest.raises(LedgerHardBlockError):
        ledger.claim_side_effecting(
            request_id,
            "send_payment",
            (),
            {"amount": 10.0},
            _payment_binding(),
        )


def test_payment_hard_blocks_failed_before_effect_retry() -> None:
    from mycelium import ActionLedger

    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    request_id = "failed-before"

    storage.set(
        LedgerEntry(
            request_id=request_id,
            tool="send_payment",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.FAILED_BEFORE_EFFECT.value,
            error="RuntimeError: gateway",
            idempotency_key=request_id,
        )
    )

    with pytest.raises(LedgerHardBlockError):
        ledger.claim_side_effecting(
            request_id,
            "send_payment",
            (),
            {"amount": 10.0},
            _payment_binding(),
        )


def test_idempotent_write_retries_failed_before_effect() -> None:
    storage = InMemoryLedgerStorage()
    binding = _idempotent_binding()
    attempts = {"count": 0}

    @ledger_sync(storage=storage, transition_binding=binding)
    def upsert_record(record_id: str) -> dict[str, str]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")
        return {"record_id": record_id, "status": "upserted"}

    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        with pytest.raises(RuntimeError):
            upsert_record(record_id="r1", tool_call_id="call_upsert")
        result = upsert_record(record_id="r1", tool_call_id="call_upsert")

    assert attempts["count"] == 2
    assert result["status"] == "upserted"


def test_idempotent_write_hard_blocks_failed_after_effect() -> None:
    from mycelium import ActionLedger

    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    request_id = "failed-after"

    storage.set(
        LedgerEntry(
            request_id=request_id,
            tool="upsert_record",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.FAILED_AFTER_EFFECT.value,
            idempotency_key=request_id,
        )
    )

    with pytest.raises(LedgerHardBlockError):
        ledger.claim_side_effecting(
            request_id,
            "upsert_record",
            (),
            {"record_id": "r1"},
            _idempotent_binding(),
        )


def test_resolve_side_effect_gate_matrix() -> None:
    in_flight = LedgerEntry(
        request_id="x",
        tool="t",
        args=[],
        kwargs={},
        status="in-flight",
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        lease_until=time.time() + 3600,
    )
    assert (
        resolve_side_effect_gate(in_flight, _payment_binding())
        == TransitionGate.POLL
    )

    expired = LedgerEntry(
        request_id="x",
        tool="t",
        args=[],
        kwargs={},
        status="in-flight",
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        lease_until=time.time() - 1,
    )
    assert (
        resolve_side_effect_gate(expired, _payment_binding())
        == TransitionGate.HARD_BLOCK
    )
