"""AF-002 task-level ledger proof suite."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from mycelium import (
    TaskFileLedgerStorage,
    TaskInMemoryLedgerStorage,
    TaskLedger,
    TaskLedgerPendingError,
    load_config_from_string,
    task_ledger,
    task_ledger_sync,
)


def test_task_ledger_deduplicates_same_task_id() -> None:
    """A completed task with the same task_id returns the stored result."""
    executions: list[str] = []

    @task_ledger_sync()
    def process_invoice(invoice_id: str) -> dict[str, Any]:
        executions.append(invoice_id)
        return {"invoice_id": invoice_id, "status": "paid"}

    r1 = process_invoice(invoice_id="inv-42", task_id="invoice-42")
    r2 = process_invoice(invoice_id="inv-42", task_id="invoice-42")

    assert len(executions) == 1
    assert r1 == r2 == {"invoice_id": "inv-42", "status": "paid"}


@pytest.mark.asyncio
async def test_task_ledger_allows_valid_repeats_with_different_task_id() -> None:
    """Same args with different task_id are legitimate separate tasks."""
    executions: list[str] = []

    @task_ledger()
    async def process_invoice(invoice_id: str) -> dict[str, Any]:
        executions.append(invoice_id)
        return {"invoice_id": invoice_id, "status": "paid"}

    await process_invoice(invoice_id="inv-42", task_id="invoice-42-run-1")
    await process_invoice(invoice_id="inv-42", task_id="invoice-42-run-2")

    assert len(executions) == 2


def test_task_ledger_id_from_business_key() -> None:
    """id_from derives a stable task id from named business-key kwargs."""
    executions: list[str] = []

    @task_ledger_sync(id_from=["invoice_id"])
    def process_invoice(invoice_id: str, amount: float) -> dict[str, Any]:
        executions.append(invoice_id)
        return {"invoice_id": invoice_id, "amount": amount}

    r1 = process_invoice(invoice_id="inv-42", amount=100.0)
    r2 = process_invoice(invoice_id="inv-42", amount=200.0)

    # Same invoice_id -> same task id -> second call returns first result.
    assert len(executions) == 1
    assert r1 == r2 == {"invoice_id": "inv-42", "amount": 100.0}


def test_task_ledger_records_failed_tasks() -> None:
    """Failed task executions are recorded for audit."""

    @task_ledger_sync()
    def flaky_task(invoice_id: str) -> dict[str, Any]:
        raise RuntimeError("payment gateway down")

    with pytest.raises(RuntimeError):
        flaky_task(invoice_id="inv-99")

    task_ledger_instance = flaky_task._mycelium_task_ledger_instance
    assert task_ledger_instance is not None
    entries = task_ledger_instance._storage.list_all()
    assert len(entries) == 1
    assert entries[0].status == "failed"
    assert "RuntimeError" in entries[0].error


def test_task_ledger_blocks_concurrent_in_flight() -> None:
    """Two concurrent attempts with the same task_id raise TaskLedgerPendingError."""
    storage = TaskInMemoryLedgerStorage()
    ledger_instance = TaskLedger(storage=storage)

    @task_ledger_sync(storage=storage)
    def slow_task(invoice_id: str) -> dict[str, Any]:
        return {"invoice_id": invoice_id}

    ledger_instance.claim(
        request_id="invoice-42",
        task="slow_task",
        args=(),
        kwargs={"invoice_id": "inv-42"},
    )

    with pytest.raises(TaskLedgerPendingError):
        slow_task(invoice_id="inv-42", task_id="invoice-42")


def test_task_ledger_file_persistence_survives_restart() -> None:
    """File-backed task ledger prevents re-execution after a restart."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "tasks.json"
        storage = TaskFileLedgerStorage(path)

        @task_ledger_sync(storage=storage)
        def finalize_order(order_id: str) -> dict[str, Any]:
            return {"order_id": order_id, "status": "confirmed"}

        finalize_order(order_id="ord-1", task_id="order-1")

        # Simulate restart with a new storage instance reading the same file.
        fresh_storage = TaskFileLedgerStorage(path)

        @task_ledger_sync(storage=fresh_storage)
        def finalize_order(order_id: str) -> dict[str, Any]:
            raise AssertionError("completed task should not re-execute")

        result = finalize_order(order_id="ord-1", task_id="order-1")
        assert result == {"order_id": "ord-1", "status": "confirmed"}


def test_task_ledger_yaml_config_apply_task() -> None:
    """load_config + apply_task wraps a task function from YAML."""
    config = load_config_from_string(
        """
tasks:
  process_refund:
    ledger:
      storage: memory
      id_from:
        - refund_id
"""
    )

    executions: list[str] = []

    @config.apply_task
    def process_refund(refund_id: str, amount: float) -> dict[str, Any]:
        executions.append(refund_id)
        return {"refund_id": refund_id, "amount": amount}

    r1 = process_refund(refund_id="ref-1", amount=50.0)
    r2 = process_refund(refund_id="ref-1", amount=75.0)

    assert len(executions) == 1
    assert r1 == r2 == {"refund_id": "ref-1", "amount": 50.0}
