from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mycelium import (
    AuditReceiptEmitter,
    AuditReceiptError,
    FileAuditReceiptStorage,
    ledger_sync,
    verify_receipt,
)


def test_emitter_signs_and_verifies_tool_receipt() -> None:
    emitter = AuditReceiptEmitter(agent_id="agent_a", signing_key="test-key")

    @ledger_sync(audit_emitter=emitter)
    def authorize_payment(amount: float, recipient: str) -> dict[str, str]:
        return {"status": "authorized"}

    authorize_payment(amount=100.0, recipient="acct_123", request_id="pay-1")

    receipts = emitter.storage.list_all()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.action == "authorize_payment"
    assert receipt.action_kind == "tool"
    assert receipt.status == "completed"
    assert verify_receipt(receipt, "test-key")


def test_tampered_receipt_fails_verification() -> None:
    emitter = AuditReceiptEmitter(agent_id="agent_a", signing_key="test-key")

    @ledger_sync(audit_emitter=emitter)
    def authorize_payment(amount: float, recipient: str) -> dict[str, str]:
        return {"status": "authorized"}

    authorize_payment(amount=50.0, recipient="acct_999", request_id="pay-2")
    receipt = emitter.storage.list_all()[0]
    tampered = receipt.from_dict({**receipt.to_dict(), "outputs": {"status": "hacked"}})
    assert not verify_receipt(tampered, "test-key")


def test_failed_tool_emits_failed_receipt() -> None:
    emitter = AuditReceiptEmitter(agent_id="agent_a", signing_key="test-key")

    @ledger_sync(audit_emitter=emitter)
    def authorize_payment(amount: float, recipient: str) -> dict[str, str]:
        raise RuntimeError("gateway down")

    with pytest.raises(RuntimeError):
        authorize_payment(amount=10.0, recipient="acct_1", request_id="pay-3")

    receipt = emitter.storage.list_all()[0]
    assert receipt.status == "failed"
    assert "RuntimeError" in (receipt.error or "")
    assert verify_receipt(receipt, "test-key")


def test_file_storage_persists_receipts() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "receipts.jsonl"
        emitter = AuditReceiptEmitter(
            agent_id="agent_a",
            signing_key="test-key",
            storage=FileAuditReceiptStorage(path),
        )

        @ledger_sync(audit_emitter=emitter)
        def authorize_payment(amount: float, recipient: str) -> dict[str, str]:
            return {"status": "authorized"}

        authorize_payment(amount=1.0, recipient="acct_1", request_id="pay-4")

        restored = AuditReceiptEmitter(
            agent_id="agent_a",
            signing_key="test-key",
            storage=FileAuditReceiptStorage(path),
        )
        assert len(restored.storage.list_all()) == 1


def test_emitter_requires_signing_key() -> None:
    with pytest.raises(AuditReceiptError):
        AuditReceiptEmitter(agent_id="agent_a", signing_key="")
