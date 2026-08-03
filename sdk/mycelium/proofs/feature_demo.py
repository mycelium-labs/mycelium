"""Feature demos for ``mycelium demo`` beyond the classic #7417 envelope proof.

Each ``prove_*`` function returns a small result dict and raises ``AssertionError``
on failure (same contract as ``langgraph_7417.prove_ledger_deduplication``).
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from typing import Any

from mycelium import (
    InMemoryLedgerStorage,
    LeaseValidity,
    LedgerEntry,
    LedgerHardBlockError,
    ReconcileResult,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionGate,
    TransitionScope,
    execution_scope,
    get_ledger,
    ledger_sync,
    record_external_operation,
    side_effect,
)
from mycelium.transition_resolution import resolve_side_effect_gate


def _mutate_binding(*, agent_id: str = "demo") -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id=agent_id,
        policy_version="2026.07.1",
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
    )


def _read_binding(*, agent_id: str = "demo") -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id=agent_id,
        policy_version="2026.07.1",
        side_effect_class=SideEffectClass.READ,
    )


def _scope() -> TransitionScope:
    return TransitionScope(thread_id="demo-t", run_id="demo-r", node="demo-n")


class _StubReconciler:
    def __init__(self, result: ReconcileResult) -> None:
        self._result = result
        self.calls: list[str] = []

    def reconcile(self, entry: Any) -> ReconcileResult:
        self.calls.append(entry.request_id)
        return self._result


def prove_return_completed() -> dict[str, Any]:
    """COMPLETED redispatch returns stored result — gate RETURN, body once."""
    storage = InMemoryLedgerStorage()
    binding = _mutate_binding(agent_id="return-demo")
    calls: list[float] = []

    @ledger_sync(storage=storage, transition_binding=binding)
    def charge(amount: float) -> dict[str, float]:
        calls.append(amount)
        return {"charged": amount}

    with execution_scope(_scope()):
        first = charge(10.0, tool_call_id="return_call")
        second = charge(10.0, tool_call_id="return_call")
        entry = storage.list_all()[0]
        gate = resolve_side_effect_gate(entry, binding)

    assert first == second == {"charged": 10.0}
    assert calls == [10.0], f"expected one body run, got {calls!r}"
    assert gate == TransitionGate.RETURN, gate
    return {
        "executions": len(calls),
        "gate": gate.value,
        "result": second,
    }


def prove_lease_auto_renew() -> dict[str, Any]:
    """Long tool keeps lease HELD via auto-renew; peer gate stays POLL."""
    storage = InMemoryLedgerStorage()
    binding = _mutate_binding(agent_id="lease-demo")
    started = threading.Event()
    release = threading.Event()
    peer_gate: list[TransitionGate] = []
    peer_validity: list[LeaseValidity] = []
    errors: list[BaseException] = []

    @ledger_sync(
        storage=storage,
        transition_binding=binding,
        lease_ttl=0.08,
        lease_renew_interval=0.02,
    )
    def slow_charge(amount: float) -> dict[str, float]:
        started.set()
        assert release.wait(timeout=2.0)
        return {"charged": amount}

    def run_owner() -> None:
        try:
            with execution_scope(_scope()):
                slow_charge(10.0, tool_call_id="lease_call")
        except BaseException as exc:  # noqa: BLE001 — surface in parent
            errors.append(exc)

    def run_peer() -> None:
        try:
            assert started.wait(timeout=2.0)
            time.sleep(0.12)  # past original ttl without renew → would be EXPIRED
            entries = storage.list_all()
            assert len(entries) == 1
            peer_validity.append(entries[0].lease_validity())
            peer_gate.append(resolve_side_effect_gate(entries[0], binding))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            release.set()

    owner = threading.Thread(target=run_owner)
    peer = threading.Thread(target=run_peer)
    owner.start()
    peer.start()
    peer.join(timeout=2.0)
    owner.join(timeout=2.0)

    assert errors == [], f"lease auto-renew demo errors: {errors!r}"
    assert peer_validity == [LeaseValidity.HELD], peer_validity
    assert peer_gate == [TransitionGate.POLL], peer_gate
    return {
        "lease_validity": peer_validity[0].value,
        "peer_gate": peer_gate[0].value,
    }


def prove_repair_gate() -> dict[str, Any]:
    """Incomplete completed record is healed on redispatch; body runs once."""
    storage = InMemoryLedgerStorage()
    binding = _mutate_binding(agent_id="repair-demo")
    calls: list[float] = []

    @ledger_sync(storage=storage, transition_binding=binding)
    def send_payment(amount: float) -> dict[str, float]:
        calls.append(amount)
        return {"charged": amount}

    with execution_scope(_scope()):
        first = send_payment(10.0, tool_call_id="repair_call")
        entries = storage.list_all()
        assert len(entries) == 1
        storage.set(replace(entries[0], idempotency_key=""))
        second = send_payment(10.0, tool_call_id="repair_call")

    healed = storage.list_all()[0]
    assert first == second == {"charged": 10.0}
    assert calls == [10.0], f"expected one body run, got {calls!r}"
    assert healed.idempotency_key, "expected repaired idempotency_key"
    return {
        "executions": len(calls),
        "repaired_idempotency_key": healed.idempotency_key,
    }


def prove_reconcile_completed() -> dict[str, Any]:
    """Provider reconcile COMPLETED → return result, no second side effect."""
    storage = InMemoryLedgerStorage()
    reconciler = _StubReconciler(ReconcileResult.completed({"charged": True}))
    calls: list[float] = []
    binding = _mutate_binding(agent_id="reconcile-demo")

    @ledger_sync(
        storage=storage,
        transition_binding=binding,
        reconciler=reconciler,
    )
    def charge(amount: float) -> dict[str, bool]:
        calls.append(amount)
        with side_effect():
            record_external_operation("pi_demo_1")
            raise RuntimeError("provider timeout")

    with execution_scope(_scope()):
        try:
            charge(amount=10.0, tool_call_id="reconcile_call")
        except RuntimeError:
            pass
        result = charge(amount=10.0, tool_call_id="reconcile_call")

    assert result == {"charged": True}
    assert calls == [10.0], f"expected no re-exec after reconcile, got {calls!r}"
    assert len(reconciler.calls) == 1
    return {
        "executions": len(calls),
        "reconcile_calls": len(reconciler.calls),
        "result": result,
    }


def prove_read_unknown_safe_retry() -> dict[str, Any]:
    """READ class: UNKNOWN may safely re-execute (unlike mutating hard-block)."""
    storage = InMemoryLedgerStorage()
    binding = _read_binding(agent_id="read-demo")
    attempts = {"count": 0}

    @ledger_sync(storage=storage, transition_binding=binding)
    def search_docs(query: str) -> dict[str, object]:
        attempts["count"] += 1
        return {"query": query, "hits": attempts["count"]}

    ledger_inst = get_ledger(search_docs)
    assert ledger_inst is not None

    with execution_scope(_scope()):
        first = search_docs(query="billing", tool_call_id="read_call")
        request_id = ledger_inst.derive_request_id(
            "search_docs",
            (),
            {"query": "billing", "tool_call_id": "read_call"},
            transition_binding=binding,
        )
        stored = ledger_inst.get(request_id)
        _set_unknown = LedgerEntry(
            request_id=stored.request_id,
            tool=stored.tool,
            args=stored.args,
            kwargs=stored.kwargs,
            status="failed",
            terminal_outcome=TerminalOutcome.UNKNOWN.value,
            error="ambiguous crash",
            started_at=stored.started_at,
            finished_at=stored.finished_at,
            owner=stored.owner,
            idempotency_key=stored.idempotency_key,
        )
        storage.set(_set_unknown)
        second = search_docs(query="billing", tool_call_id="read_call")

    assert attempts["count"] == 2
    assert first == {"query": "billing", "hits": 1}
    assert second == {"query": "billing", "hits": 2}
    return {"executions": attempts["count"], "class": "read"}


def prove_hard_block() -> dict[str, Any]:
    """Ambiguous mutate cannot re-execute: redispatch raises LedgerHardBlockError."""
    storage = InMemoryLedgerStorage()
    binding = _mutate_binding(agent_id="hardblock-demo")
    calls: list[float] = []

    @ledger_sync(storage=storage, transition_binding=binding)
    def charge(amount: float) -> dict[str, bool]:
        calls.append(amount)
        with side_effect():
            record_external_operation("pi_hardblock_demo")
            raise RuntimeError("provider timeout")

    with execution_scope(_scope()):
        try:
            charge(amount=10.0, tool_call_id="hardblock_call")
        except RuntimeError:
            pass
        entries = storage.list_all()
        assert len(entries) == 1, f"expected one ledger entry after fail, got {entries!r}"
        request_id = entries[0].request_id
        try:
            charge(amount=10.0, tool_call_id="hardblock_call")
            raise AssertionError("expected LedgerHardBlockError on ambiguous redispatch")
        except LedgerHardBlockError as exc:
            message = str(exc)
        final = storage.get(request_id)
        assert final is not None

    assert calls == [10.0], f"expected no re-exec after hard block, got {calls!r}"
    assert final.resolved_terminal_outcome() == TerminalOutcome.UNKNOWN
    return {
        "executions": len(calls),
        "gate": TransitionGate.HARD_BLOCK.value,
        "raised": "LedgerHardBlockError",
        "terminal_outcome": final.resolved_terminal_outcome().value,
        "message": message,
    }


def prove_operator_release() -> dict[str, Any]:
    """Hard-block after ambiguous mutate, operator release, one re-exec."""
    storage = InMemoryLedgerStorage()
    binding = _mutate_binding(agent_id="release-demo")
    calls: list[float] = []
    fail_first = {"v": True}

    @ledger_sync(storage=storage, transition_binding=binding)
    def charge(amount: float) -> dict[str, bool]:
        calls.append(amount)
        with side_effect():
            record_external_operation("pi_release_demo")
            if fail_first["v"]:
                fail_first["v"] = False
                raise RuntimeError("provider timeout")
            return {"charged": True}

    ledger_inst = get_ledger(charge)
    assert ledger_inst is not None

    with execution_scope(_scope()):
        try:
            charge(amount=10.0, tool_call_id="release_call")
        except RuntimeError:
            pass
        entries = storage.list_all()
        assert len(entries) == 1, f"expected one ledger entry after fail, got {entries!r}"
        request_id = entries[0].request_id
        try:
            charge(amount=10.0, tool_call_id="release_call")
            raise AssertionError("expected LedgerHardBlockError before release")
        except LedgerHardBlockError:
            pass

        entry = ledger_inst.release(
            request_id,
            verified="not_executed",
            by="demo-ops",
            reason="provider shows no charge for pi_release_demo",
        )
        assert entry.operator_resolution == "not_executed"

        result = charge(amount=10.0, tool_call_id="release_call")
        again = charge(amount=10.0, tool_call_id="release_call")

    assert result == again == {"charged": True}
    assert calls == [10.0, 10.0], f"expected fail + one re-exec, got {calls!r}"
    final = storage.get(request_id)
    assert final is not None
    assert final.resolved_terminal_outcome() == TerminalOutcome.COMPLETED
    return {
        "executions": len(calls),
        "operator_resolution_applied": "not_executed",
        "final_outcome": final.resolved_terminal_outcome().value,
    }
