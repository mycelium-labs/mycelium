"""Tests for read-only transition resolution: poll, reclaim, retry."""

from __future__ import annotations

import threading
import time

import pytest

from mycelium import (
    ActionLedger,
    InMemoryLedgerStorage,
    LedgerEntry,
    LedgerPendingError,
    LedgerPollTimeoutError,
    LedgerSoftBlockError,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    execution_scope,
    get_ledger,
    ledger_sync,
)
from mycelium.transition_resolution import TransitionGate, resolve_read_only_gate


def _read_only_binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo-agent",
        policy_version="2026.07.1",
        side_effect_class=SideEffectClass.READ,
    )


def test_read_only_polls_until_completed() -> None:
    storage = InMemoryLedgerStorage()
    binding = _read_only_binding()
    executions: list[str] = []
    started = threading.Event()

    @ledger_sync(storage=storage, transition_binding=binding, poll_interval=0.01)
    def search_docs(query: str) -> dict[str, str]:
        started.set()
        time.sleep(0.05)
        executions.append(query)
        return {"query": query, "hits": 1}

    results: list[dict[str, str]] = []

    def worker() -> None:
        with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
            results.append(
                search_docs(query="billing", tool_call_id="call_poll")
            )

    first = threading.Thread(target=worker)
    second = threading.Thread(target=worker)
    first.start()
    assert started.wait(timeout=1.0)
    second.start()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert len(executions) == 1
    assert results[0] == results[1] == {"query": "billing", "hits": 1}


def test_read_only_reclaims_expired_lease() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage, lease_ttl=0.05, poll_interval=0.01)
    request_id = "expired-key"
    stale = LedgerEntry(
        request_id=request_id,
        tool="search_docs",
        args=[],
        kwargs={"query": "billing"},
        status="in-flight",
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        lease_until=time.time() - 1,
        idempotency_key=request_id,
    )
    storage.set(stale)

    claimed = ledger.claim_read_only(
        request_id,
        "search_docs",
        (),
        {"query": "billing"},
    )
    assert claimed.status == "in-flight"
    stored = storage.get(request_id)
    assert stored is not None
    assert stored.lease_until is not None
    assert stored.lease_until > time.time()


def test_read_only_retries_after_failure() -> None:
    storage = InMemoryLedgerStorage()
    binding = _read_only_binding()
    attempts = {"count": 0}

    @ledger_sync(storage=storage, transition_binding=binding)
    def search_docs(query: str) -> dict[str, str]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")
        return {"query": query, "hits": 2}

    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        with pytest.raises(RuntimeError):
            search_docs(query="billing", tool_call_id="call_retry")
        result = search_docs(query="billing", tool_call_id="call_retry")

    assert attempts["count"] == 2
    assert result == {"query": "billing", "hits": 2}


def test_non_read_only_without_transition_still_raises_pending_error() -> None:
    storage = InMemoryLedgerStorage()
    started = threading.Event()
    release = threading.Event()

    @ledger_sync(storage=storage)
    def send_payment(amount: float) -> dict[str, str]:
        started.set()
        assert release.wait(timeout=1.0)
        return {"status": "sent"}

    def hold_claim() -> None:
        send_payment(amount=10.0, tool_call_id="call_pay")

    holder = threading.Thread(target=hold_claim)
    holder.start()
    assert started.wait(timeout=1.0)

    with pytest.raises(LedgerPendingError):
        send_payment(amount=10.0, tool_call_id="call_pay")

    release.set()
    holder.join(timeout=2.0)


def test_read_only_poll_timeout() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(
        storage=storage,
        lease_ttl=3600.0,
        poll_interval=0.01,
        poll_timeout=0.05,
    )
    storage.set(
        LedgerEntry(
            request_id="stuck",
            tool="search_docs",
            args=[],
            kwargs={},
            status="in-flight",
            terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
            lease_until=time.time() + 3600,
            idempotency_key="stuck",
        )
    )

    with pytest.raises(LedgerPollTimeoutError):
        ledger.claim_read_only("stuck", "search_docs", (), {})


def _entry(
    outcome: TerminalOutcome,
    *,
    lease_until: float | None = None,
) -> LedgerEntry:
    return LedgerEntry(
        request_id="ro",
        tool="search_docs",
        args=[],
        kwargs={"query": "billing"},
        status="in-flight",
        terminal_outcome=outcome.value,
        lease_until=lease_until,
        idempotency_key="ro",
    )


def test_resolve_read_only_gate_matrix() -> None:
    assert (
        resolve_read_only_gate(_entry(TerminalOutcome.COMPLETED))
        == TransitionGate.RETURN
    )
    assert (
        resolve_read_only_gate(
            _entry(TerminalOutcome.IN_FLIGHT, lease_until=time.time() + 100)
        )
        == TransitionGate.POLL
    )
    # A stale in-flight lease resolves to EXPIRED -> safe to reclaim.
    assert (
        resolve_read_only_gate(
            _entry(TerminalOutcome.IN_FLIGHT, lease_until=time.time() - 1)
        )
        == TransitionGate.RECLAIM
    )
    assert (
        resolve_read_only_gate(_entry(TerminalOutcome.FAILED_BEFORE_EFFECT))
        == TransitionGate.RECLAIM
    )
    assert (
        resolve_read_only_gate(_entry(TerminalOutcome.UNKNOWN))
        == TransitionGate.SOFT_BLOCK
    )
    assert (
        resolve_read_only_gate(_entry(TerminalOutcome.BLOCKED))
        == TransitionGate.SOFT_BLOCK
    )


def test_read_only_unknown_soft_block_retries_by_default() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage, poll_interval=0.01)
    request_id = "ro-unknown"
    storage.set(
        LedgerEntry(
            request_id=request_id,
            tool="search_docs",
            args=[],
            kwargs={"query": "billing"},
            status="failed",
            terminal_outcome=TerminalOutcome.UNKNOWN.value,
            idempotency_key=request_id,
        )
    )

    claimed = ledger.claim_read_only(
        request_id, "search_docs", (), {"query": "billing"}
    )

    # Reversible read is reset to a fresh in-flight claim so it runs once more.
    assert claimed.status == "in-flight"
    assert claimed.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT


def test_read_only_unknown_soft_block_defers_when_configured() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage, defer_read_only_unknown=True)
    request_id = "ro-unknown-defer"
    storage.set(
        LedgerEntry(
            request_id=request_id,
            tool="search_docs",
            args=[],
            kwargs={"query": "billing"},
            status="failed",
            terminal_outcome=TerminalOutcome.UNKNOWN.value,
            idempotency_key=request_id,
        )
    )

    with pytest.raises(LedgerSoftBlockError):
        ledger.claim_read_only(request_id, "search_docs", (), {"query": "billing"})


def test_read_only_unknown_reexecutes_via_decorator() -> None:
    storage = InMemoryLedgerStorage()
    binding = _read_only_binding()
    attempts = {"count": 0}

    @ledger_sync(storage=storage, transition_binding=binding)
    def search_docs(query: str) -> dict[str, object]:
        attempts["count"] += 1
        return {"query": query, "hits": attempts["count"]}

    inner = get_ledger(search_docs)
    assert inner is not None

    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        first = search_docs(query="billing", tool_call_id="call_ro")
        assert first == {"query": "billing", "hits": 1}

        # Simulate an ambiguous crash-after-claim recorded as UNKNOWN.
        request_id = inner.derive_request_id(
            "search_docs",
            (),
            {"query": "billing", "tool_call_id": "call_ro"},
            transition_binding=binding,
        )
        inner.mark_unknown(request_id, error="ambiguous crash")

        second = search_docs(query="billing", tool_call_id="call_ro")

    assert attempts["count"] == 2
    assert second == {"query": "billing", "hits": 2}
