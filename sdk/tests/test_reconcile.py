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
