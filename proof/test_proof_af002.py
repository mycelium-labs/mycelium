"""AF-002 proof suite — fixtures grounded in real GitHub issues."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from mycelium import (
    ActionLedger,
    FileLedgerStorage,
    InMemoryLedgerStorage,
    LedgerPendingError,
    Session,
    ledger,
    ledger_sync,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "af002"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text())


def test_ledger_deduplicates_redispatched_tool_call_langgraph_7417() -> None:
    """Long tool call redispatched with the same tool_call_id executes only once."""
    fixture = load_fixture("langgraph-7417-duplicate-tool-execution.json")
    tool_call_id = fixture["scenario"]["tool_call_id"]
    executions: list[dict[str, Any]] = []

    @ledger_sync()
    def subagent_task(task: str, duration_seconds: int) -> dict[str, Any]:
        executions.append({"task": task, "duration_seconds": duration_seconds})
        return {"task": task, "result": "done"}

    # Cloud redispatches the same call while the original is still in flight.
    r1 = subagent_task(task="analyze_market", duration_seconds=300, tool_call_id=tool_call_id)
    r2 = subagent_task(task="analyze_market", duration_seconds=300, tool_call_id=tool_call_id)

    assert len(executions) == 1
    assert r1 == r2 == {"task": "analyze_market", "result": "done"}


@pytest.mark.asyncio
async def test_ledger_deduplicates_task_retry_crewai_5802() -> None:
    """CrewAI task retry with the same request_id does not re-execute the side effect."""
    fixture = load_fixture("crewai-5802-retry-idempotency.json")
    scenario = fixture["scenario"]
    amount = scenario["args"]["amount"]
    recipient = scenario["args"]["recipient"]
    request_id = "payment-acct_123-100.0"
    executions: list[dict[str, Any]] = []

    @ledger()
    async def send_payment(amount: float, recipient: str) -> dict[str, Any]:
        executions.append({"amount": amount, "recipient": recipient})
        return {"status": "sent"}

    # First attempt succeeds.
    first = await send_payment(amount=amount, recipient=recipient, request_id=request_id)
    assert first == {"status": "sent"}
    assert len(executions) == 1

    # Retry arrives with the same business request id.
    retry = await send_payment(amount=amount, recipient=recipient, request_id=request_id)
    assert retry == {"status": "sent"}
    assert len(executions) == 1  # no duplicate side effect


def test_ledger_blocks_concurrent_in_flight_attempts() -> None:
    """Two concurrent attempts with the same request_id raise LedgerPendingError."""
    storage = InMemoryLedgerStorage()
    ledger_instance = ActionLedger(storage=storage)

    @ledger_sync(storage=storage)
    def slow_payment(amount: float, recipient: str) -> dict[str, Any]:
        return {"status": "sent"}

    # Manually claim the key to simulate an in-flight execution.
    ledger_instance.claim(
        request_id="in-flight-payment",
        tool="slow_payment",
        args=(),
        kwargs={"amount": 50.0, "recipient": "acct_999"},
    )

    with pytest.raises(LedgerPendingError):
        slow_payment(amount=50.0, recipient="acct_999", request_id="in-flight-payment")


def test_ledger_records_failed_attempts_for_audit() -> None:
    """Failed tool calls are recorded in the ledger with error details."""

    @ledger_sync()
    def flaky_payment(amount: float, recipient: str) -> dict[str, Any]:
        raise RuntimeError("payment gateway timeout")

    with pytest.raises(RuntimeError):
        flaky_payment(amount=25.0, recipient="acct_777")

    action_ledger = flaky_payment._mycelium_ledger_instance
    assert action_ledger is not None
    entries = action_ledger._storage.list_all()
    assert len(entries) == 1
    assert entries[0].status == "failed"
    assert "RuntimeError" in entries[0].error


def test_ledger_allows_valid_repeats_with_different_request_id() -> None:
    """Same args with different request_id are separate, legitimate operations."""
    executions: list[str] = []

    @ledger_sync()
    def send_payment(amount: float, recipient: str) -> dict[str, Any]:
        executions.append(f"{amount}:{recipient}")
        return {"status": "sent"}

    send_payment(amount=10.0, recipient="acct_111", request_id="payment-1")
    send_payment(amount=10.0, recipient="acct_111", request_id="payment-2")

    assert len(executions) == 2


def test_ledger_file_persistence_survives_new_instance() -> None:
    """File-backed ledger survives a new ActionLedger instance (process restart simulation)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "ledger.json"
        storage = FileLedgerStorage(path)

        @ledger_sync(storage=storage)
        def create_order(product_id: str, quantity: int) -> dict[str, Any]:
            return {"order_id": "ord-1", "product_id": product_id, "quantity": quantity}

        create_order(product_id="sku-42", quantity=2, request_id="order-1")

        # Simulate restart by creating a fresh ActionLedger reading the same file.
        fresh_storage = FileLedgerStorage(path)

        @ledger_sync(storage=fresh_storage)
        def create_order(product_id: str, quantity: int) -> dict[str, Any]:
            raise AssertionError("should not re-execute a completed request")

        result = create_order(product_id="sku-42", quantity=2, request_id="order-1")
        assert result == {"order_id": "ord-1", "product_id": "sku-42", "quantity": 2}


def test_ledger_session_dedup_for_code_called_tools() -> None:
    """Within a Session, identical code-level calls are deduplicated."""
    executions: list[tuple[float, str]] = []

    @ledger_sync()
    def fetch_rate(currency: str) -> dict[str, Any]:
        executions.append((currency,))
        return {"currency": currency, "rate": 1.25}

    with Session():
        r1 = fetch_rate(currency="EUR")
        r2 = fetch_rate(currency="EUR")

    assert len(executions) == 1
    assert r1 == r2
