"""Tests for the reconciliation loop (v1.7.0, Phase 2).

An ambiguous side-effecting transition (``UNKNOWN`` / ``maybe_crossed``) that
recorded an ``external_operation_ref`` can be resolved automatically by a
``Reconciler`` instead of hard-blocking:

- ``COMPLETED`` → redispatch returns the reconciled result, no re-execution.
- ``NOT_EXECUTED`` → the tool is allowed to run exactly once more.
- ``UNKNOWN`` / reconciler error / no ref / no reconciler → hard-block.
"""

from __future__ import annotations

import asyncio

import pytest

from mycelium import (
    InMemoryLedgerStorage,
    LedgerHardBlockError,
    ReconcileResult,
    ReconcileStatus,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    execution_scope,
    ledger,
    ledger_sync,
    record_external_operation,
    side_effect,
)


def _binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
    )


class StubReconciler:
    def __init__(self, result: ReconcileResult) -> None:
        self._result = result
        self.calls: list[str] = []

    def reconcile(self, entry) -> ReconcileResult:
        self.calls.append(entry.request_id)
        return self._result


class RaisingReconciler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def reconcile(self, entry) -> ReconcileResult:
        self.calls.append(entry.request_id)
        raise RuntimeError("provider unreachable")


class AsyncReconciler:
    def __init__(self, result: ReconcileResult) -> None:
        self._result = result
        self.calls: list[str] = []

    async def reconcile_async(self, entry) -> ReconcileResult:
        self.calls.append(entry.request_id)
        return self._result

    def reconcile(self, entry) -> ReconcileResult:  # pragma: no cover
        raise AssertionError("async path should prefer reconcile_async")


def _scope() -> TransitionScope:
    return TransitionScope(thread_id="t1", run_id="r1")


def test_reconcile_result_constructors() -> None:
    assert ReconcileResult.completed("x") == ReconcileResult(
        ReconcileStatus.COMPLETED, "x"
    )
    assert ReconcileResult.not_executed().status == ReconcileStatus.NOT_EXECUTED
    assert ReconcileResult.unknown().status == ReconcileStatus.UNKNOWN


def test_reconcile_completed_returns_result_without_reexec() -> None:
    storage = InMemoryLedgerStorage()
    reconciler = StubReconciler(ReconcileResult.completed({"charged": True}))
    calls: list[float] = []

    @ledger_sync(storage=storage, transition_binding=_binding(), reconciler=reconciler)
    def charge(amount: float) -> dict[str, bool]:
        calls.append(amount)
        with side_effect():
            record_external_operation("pi_1")
            raise RuntimeError("provider timeout")

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            charge(amount=10.0, tool_call_id="c1")

        result = charge(amount=10.0, tool_call_id="c1")

    assert result == {"charged": True}
    assert len(calls) == 1  # body not re-executed
    assert len(reconciler.calls) == 1
    entry = storage.get(reconciler.calls[0])
    assert entry.resolved_terminal_outcome() == TerminalOutcome.COMPLETED
    assert entry.result == {"charged": True}


def test_reconcile_not_executed_allows_single_reexec() -> None:
    storage = InMemoryLedgerStorage()
    reconciler = StubReconciler(ReconcileResult.not_executed())
    calls: list[float] = []
    fail_first = {"v": True}

    @ledger_sync(storage=storage, transition_binding=_binding(), reconciler=reconciler)
    def charge(amount: float) -> dict[str, bool]:
        calls.append(amount)
        with side_effect():
            record_external_operation("pi_2")
            if fail_first["v"]:
                fail_first["v"] = False
                raise RuntimeError("provider timeout")
            return {"charged": True}

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            charge(amount=10.0, tool_call_id="c1")

        result = charge(amount=10.0, tool_call_id="c1")

    assert result == {"charged": True}
    assert len(calls) == 2  # reconcile confirmed nothing happened; re-ran once
    assert len(reconciler.calls) == 1
    entry = storage.get(reconciler.calls[0])
    assert entry.resolved_terminal_outcome() == TerminalOutcome.COMPLETED


def test_reconcile_unknown_hard_blocks() -> None:
    storage = InMemoryLedgerStorage()
    reconciler = StubReconciler(ReconcileResult.unknown())

    @ledger_sync(storage=storage, transition_binding=_binding(), reconciler=reconciler)
    def charge(amount: float) -> dict[str, bool]:
        with side_effect():
            record_external_operation("pi_3")
            raise RuntimeError("provider timeout")

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            charge(amount=10.0, tool_call_id="c1")

        with pytest.raises(LedgerHardBlockError, match="pi_3"):
            charge(amount=10.0, tool_call_id="c1")

    assert len(reconciler.calls) == 1


def test_reconcile_failure_is_fail_closed() -> None:
    storage = InMemoryLedgerStorage()
    reconciler = RaisingReconciler()

    @ledger_sync(storage=storage, transition_binding=_binding(), reconciler=reconciler)
    def charge(amount: float) -> dict[str, bool]:
        with side_effect():
            record_external_operation("pi_4")
            raise RuntimeError("provider timeout")

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            charge(amount=10.0, tool_call_id="c1")

        with pytest.raises(LedgerHardBlockError):
            charge(amount=10.0, tool_call_id="c1")

    assert len(reconciler.calls) == 1


def test_reconcile_skipped_without_external_ref() -> None:
    storage = InMemoryLedgerStorage()
    reconciler = StubReconciler(ReconcileResult.completed("should-not-be-used"))

    @ledger_sync(storage=storage, transition_binding=_binding(), reconciler=reconciler)
    def charge(amount: float) -> dict[str, bool]:
        with side_effect():
            raise RuntimeError("provider timeout")

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            charge(amount=10.0, tool_call_id="c1")

        with pytest.raises(LedgerHardBlockError):
            charge(amount=10.0, tool_call_id="c1")

    assert reconciler.calls == []  # no ref means no provider lookup


def test_hard_block_without_reconciler_still_blocks() -> None:
    storage = InMemoryLedgerStorage()

    @ledger_sync(storage=storage, transition_binding=_binding())
    def charge(amount: float) -> dict[str, bool]:
        with side_effect():
            record_external_operation("pi_5")
            raise RuntimeError("provider timeout")

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            charge(amount=10.0, tool_call_id="c1")

        with pytest.raises(LedgerHardBlockError, match="pi_5"):
            charge(amount=10.0, tool_call_id="c1")


def test_async_reconcile_completed_returns_result() -> None:
    storage = InMemoryLedgerStorage()
    reconciler = AsyncReconciler(ReconcileResult.completed({"charged": True}))
    calls: list[float] = []

    @ledger(storage=storage, transition_binding=_binding(), reconciler=reconciler)
    async def charge(amount: float) -> dict[str, bool]:
        calls.append(amount)
        with side_effect():
            record_external_operation("pi_async")
            raise RuntimeError("provider timeout")

    async def run() -> dict[str, bool]:
        with execution_scope(_scope()):
            with pytest.raises(RuntimeError):
                await charge(amount=10.0, tool_call_id="c1")
            return await charge(amount=10.0, tool_call_id="c1")

    result = asyncio.run(run())

    assert result == {"charged": True}
    assert len(calls) == 1
    assert len(reconciler.calls) == 1


def test_expired_not_crossed_reclaims_when_reconcile_proves_not_executed() -> None:
    """EXPIRED + not_crossed + external_operation_ref + NOT_EXECUTED → reclaim.

    Strict (payment) classes hard-block EXPIRED at the gate; reclaim is only
    allowed when a Reconciler proves the provider never executed the effect.
    """
    import time

    from mycelium import LedgerEntry, SideEffectBoundary, get_ledger

    storage = InMemoryLedgerStorage()
    reconciler = StubReconciler(ReconcileResult.not_executed())
    calls: list[float] = []

    @ledger_sync(
        storage=storage,
        transition_binding=_binding(),
        reconciler=reconciler,
        lease_ttl=3600.0,
    )
    def charge(amount: float) -> dict[str, bool]:
        calls.append(amount)
        return {"charged": True}

    ledger_inst = get_ledger(charge)
    assert ledger_inst is not None

    with execution_scope(_scope()):
        # Seed a stale in-flight claim that never crossed the side-effect
        # boundary but recorded a provider handle before the worker died.
        request_id = ledger_inst.derive_request_id(
            "charge",
            (),
            {"amount": 10.0, "tool_call_id": "c_expired"},
            transition_binding=_binding(),
        )
        storage.set(
            LedgerEntry(
                request_id=request_id,
                tool="charge",
                args=[],
                kwargs={"amount": 10.0},
                status="in-flight",
                terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
                lease_until=time.time() - 1,
                side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
                external_operation_ref="pi_expired_1",
                idempotency_key=request_id,
            )
        )

        result = charge(amount=10.0, tool_call_id="c_expired")

    assert result == {"charged": True}
    assert calls == [10.0]
    assert reconciler.calls == [request_id]
    entry = storage.get(request_id)
    assert entry is not None
    assert entry.resolved_terminal_outcome() == TerminalOutcome.COMPLETED


def test_expired_not_crossed_hard_blocks_without_external_ref() -> None:
    """EXPIRED + not_crossed without external_operation_ref is not provable."""
    import time

    from mycelium import LedgerEntry, SideEffectBoundary, get_ledger

    storage = InMemoryLedgerStorage()
    reconciler = StubReconciler(ReconcileResult.not_executed())

    @ledger_sync(
        storage=storage,
        transition_binding=_binding(),
        reconciler=reconciler,
    )
    def charge(amount: float) -> dict[str, bool]:
        return {"charged": True}

    ledger_inst = get_ledger(charge)
    assert ledger_inst is not None

    with execution_scope(_scope()):
        request_id = ledger_inst.derive_request_id(
            "charge",
            (),
            {"amount": 10.0, "tool_call_id": "c_expired_noref"},
            transition_binding=_binding(),
        )
        storage.set(
            LedgerEntry(
                request_id=request_id,
                tool="charge",
                args=[],
                kwargs={"amount": 10.0},
                status="in-flight",
                terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
                lease_until=time.time() - 1,
                side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
                external_operation_ref=None,
                idempotency_key=request_id,
            )
        )

        with pytest.raises(LedgerHardBlockError, match="not_crossed"):
            charge(amount=10.0, tool_call_id="c_expired_noref")

    assert reconciler.calls == []  # no ref → no provider lookup


def test_expired_after_poll_reconciles_instead_of_blind_hard_block() -> None:
    """Poll returns on EXPIRED so the claim loop can reconcile before blocking.

    Previously ``_poll_side_effecting`` raised ``LedgerHardBlockError`` on
    EXPIRED without consulting the Reconciler.
    """
    import time
    from dataclasses import replace

    from mycelium import ActionLedger, LedgerEntry, SideEffectBoundary
    from mycelium.transition_resolution import TransitionGate, resolve_side_effect_gate

    storage = InMemoryLedgerStorage()
    reconciler = StubReconciler(ReconcileResult.not_executed())
    ledger = ActionLedger(
        storage=storage,
        reconciler=reconciler,
        poll_interval=0.01,
        poll_timeout=1.0,
        lease_ttl=3600.0,
    )
    binding = _binding()
    request_id = "expired-after-poll"

    storage.set(
        LedgerEntry(
            request_id=request_id,
            tool="charge",
            args=[],
            kwargs={"amount": 10.0},
            status="in-flight",
            terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
            lease_until=time.time() + 3600,  # valid → POLL
            side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
            external_operation_ref="pi_poll_expired",
            idempotency_key=request_id,
        )
    )
    assert (
        resolve_side_effect_gate(storage.get(request_id), binding)
        == TransitionGate.POLL
    )

    # Expire the lease mid-poll observation window.
    current = storage.get(request_id)
    assert current is not None
    storage.set(replace(current, lease_until=time.time() - 1))

    # Poll must return (not raise) so the outer claim can reconcile.
    ledger._poll_side_effecting(
        request_id,
        tool="charge",
        interval=0.01,
        poll_deadline=time.time() + 1.0,
    )

    claimed = ledger.claim_side_effecting(
        request_id,
        "charge",
        (),
        {"amount": 10.0},
        binding,
    )

    assert reconciler.calls == [request_id]
    # Reconcile NOT_EXECUTED resets to a fresh in-flight claim (tool may run once).
    assert claimed.status == "in-flight"
    assert claimed.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT
    assert claimed.external_operation_ref is None  # fresh claim
