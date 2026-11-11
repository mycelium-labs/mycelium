"""Signed audit receipt round trip: emit, persist, reload, verify, tamper.

Emits a receipt for a fake tool action, persists it to a temporary JSONL file,
reloads it, and verifies it with a throwaway signing key generated for this run.
No credentials are read and no signing key is printed.

Run from the sdk directory::

    python examples/audit_receipt_roundtrip.py

What a valid receipt proves: the signed fields were not changed after signing,
relative to whoever holds the shared HMAC key. It does not prove an independent
provider attestation, and it does not prove the action ran exactly once.
"""

from __future__ import annotations

import dataclasses
import secrets
import tempfile
from pathlib import Path

from mycelium.action_ledger import LedgerEntry
from mycelium.audit_receipt import (
    AuditReceiptEmitter,
    FileAuditReceiptStorage,
    verify_receipt,
)


def main() -> None:
    signing_key = secrets.token_hex(32)  # throwaway; never printed or stored
    wrong_key = secrets.token_hex(32)

    entry = LedgerEntry(
        request_id="req-demo-1",
        tool="send_invoice",
        args=["INV-1001"],  # lists, not tuples: JSON reload returns lists
        kwargs={"customer": "cust_demo"},
        status="completed",
        idempotency_key="req-demo-1",
        result={"invoice_id": "inv_fake_1", "amount": "10.00"},
    )

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "receipts.jsonl"
        emitter = AuditReceiptEmitter(
            agent_id="demo-agent",
            signing_key=signing_key,
            storage=FileAuditReceiptStorage(log_path),
        )
        emitted = emitter.emit_from_tool_entry(entry)

        # Reload from disk in a fresh storage object, as a verifier would.
        reloaded = FileAuditReceiptStorage(log_path).list_all()
        assert len(reloaded) == 1, "expected exactly one persisted receipt"
        original = reloaded[0]

        original_ok = verify_receipt(original, signing_key)
        forged_outputs = {"invoice_id": "inv_fake_1", "amount": "0.01"}
        tampered = dataclasses.replace(original, outputs=forged_outputs)
        tampered_ok = verify_receipt(tampered, signing_key)
        wrong_key_ok = verify_receipt(original, wrong_key)

    assert original == emitted, "reloaded receipt must match the emitted receipt"
    print(f"receipt {original.receipt_id} for {original.action} ({original.status})")
    print(f"original verifies after reload: {original_ok}")
    print(f"copy with changed signed field verifies: {tampered_ok}")
    print(f"wrong key verifies: {wrong_key_ok}")
    print("note: a valid HMAC proves integrity relative to the shared key only,")
    print("      not independent provider attestation or exactly-once execution.")
    print(
        "round-trip ok: original=%s tampered=%s wrong_key=%s"
        % (original_ok, tampered_ok, wrong_key_ok)
    )


if __name__ == "__main__":
    main()
