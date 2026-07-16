"""Tests for terminal_outcome and ledger envelope fields."""

from __future__ import annotations

import time

import pytest

from mycelium import (
    ActionLedger,
    InMemoryLedgerStorage,
    LedgerEntry,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    execution_scope,
    ledger_sync,
)
from mycelium.transition import terminal_from_legacy_status


def test_ledger_entry_sets_envelope_on_claim() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    entry = ledger.claim("key-1", "search_docs", (), {"query": "x"})

    assert entry.terminal_outcome == TerminalOutcome.IN_FLIGHT.value
    assert entry.idempotency_key == "key-1"
    assert entry.owner is not None
    assert entry.lease_until is not None


def test_complete_sets_completed_terminal_outcome() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    ledger.claim("key-2", "search_docs", (), {})
    completed = ledger.complete("key-2", {"hits": 1})

    assert completed.terminal_outcome == TerminalOutcome.COMPLETED.value
    assert completed.status == "completed"
    assert completed.lease_until is None


def test_fail_defaults_to_failed_before_effect() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    ledger.claim("key-3", "search_docs", (), {})
    failed = ledger.fail("key-3", RuntimeError("boom"))

    assert failed.terminal_outcome == TerminalOutcome.FAILED_BEFORE_EFFECT.value
    assert failed.status == "failed"


def test_fail_after_effect() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    ledger.claim("key-4", "send_payment", (), {})
    failed = ledger.fail("key-4", RuntimeError("charged"), failed_after_effect=True)

    assert failed.terminal_outcome == TerminalOutcome.FAILED_AFTER_EFFECT.value


def test_expired_resolved_from_stale_in_flight() -> None:
    entry = LedgerEntry(
        request_id="key-5",
        tool="search_docs",
        args=[],
        kwargs={},
        status="in-flight",
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        lease_until=time.time() - 1,
    )
    assert entry.resolved_terminal_outcome() == TerminalOutcome.EXPIRED
    assert entry.is_reclaimable()


def test_from_dict_migrates_legacy_status() -> None:
    legacy = {
        "request_id": "legacy-1",
        "tool": "search_docs",
        "args": [],
        "kwargs": {},
        "status": "completed",
        "result": {"ok": True},
    }
    entry = LedgerEntry.from_dict(legacy)
    assert entry.terminal_outcome == TerminalOutcome.COMPLETED.value
    assert entry.idempotency_key == "legacy-1"


def test_mark_blocked_and_unknown() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    ledger.claim("key-6", "search_docs", (), {})

    blocked = ledger.mark_blocked("key-6", error="policy")
    assert blocked.terminal_outcome == TerminalOutcome.BLOCKED.value

    ledger.claim("key-7", "search_docs", (), {})
    unknown = ledger.mark_unknown("key-7", error="orphan")
    assert unknown.terminal_outcome == TerminalOutcome.UNKNOWN.value


def test_reclaim_after_expired_lease() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage, lease_ttl=0.05)
    stale = LedgerEntry(
        request_id="key-8",
        tool="search_docs",
        args=[],
        kwargs={},
        status="in-flight",
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        lease_until=time.time() - 1,
        idempotency_key="key-8",
    )
    storage.set(stale)

    reclaimed = ledger.claim_read_only("key-8", "search_docs", (), {})
    assert reclaimed.terminal_outcome == TerminalOutcome.IN_FLIGHT.value
    assert reclaimed.lease_until is not None
    assert reclaimed.lease_until > time.time()


def test_read_only_retry_after_failed_before_effect() -> None:
    binding = ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.READ,
    )
    storage = InMemoryLedgerStorage()
    attempts = {"count": 0}

    @ledger_sync(storage=storage, transition_binding=binding)
    def search_docs(query: str) -> dict[str, str]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")
        return {"query": query}

    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        with pytest.raises(RuntimeError):
            search_docs(query="x", tool_call_id="call-1")
        search_docs(query="x", tool_call_id="call-1")

    entry = storage.get(list(storage._entries.keys())[0])
    assert entry is not None
    assert entry.terminal_outcome == TerminalOutcome.COMPLETED.value


def test_terminal_from_legacy_status_mapping() -> None:
    assert terminal_from_legacy_status("completed") == TerminalOutcome.COMPLETED
    assert terminal_from_legacy_status("failed") == TerminalOutcome.FAILED_BEFORE_EFFECT
    assert terminal_from_legacy_status("in-flight") == TerminalOutcome.IN_FLIGHT
