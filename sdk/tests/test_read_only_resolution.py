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
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    execution_scope,
    ledger_sync,
)


def _read_only_binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo-agent",
        policy_version="2026.07.1",
        side_effect_class=SideEffectClass.READ_ONLY,
    )


def test_read_only_polls_until_completed() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage, poll_interval=0.01)
    binding = _read_only_binding()
    executions: list[str] = []
    started = threading.Event()

    @ledger_sync(storage=storage, transition_binding=binding)
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
    ledger = ActionLedger(storage=storage)
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
