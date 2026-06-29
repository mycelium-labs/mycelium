"""AuditReceipt — AF-002 tamper-evident signed action receipts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mycelium.action_ledger import LedgerEntry
from mycelium.task_ledger import TaskLedgerEntry


class AuditReceiptError(Exception):
    """Raised when a receipt cannot be created or verified."""


@dataclass(frozen=True)
class AuditReceiptRecord:
    """Signed, structured proof of a consequential agent action."""

    receipt_id: str
    agent_id: str
    action: str
    action_kind: str  # "tool" | "task"
    request_id: str
    inputs: dict[str, Any]
    outputs: Any
    status: str
    timestamp: float
    signature: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "agent_id": self.agent_id,
            "action": self.action,
            "action_kind": self.action_kind,
            "request_id": self.request_id,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "status": self.status,
            "timestamp": self.timestamp,
            "signature": self.signature,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditReceiptRecord:
        return cls(
            receipt_id=str(data["receipt_id"]),
            agent_id=str(data["agent_id"]),
            action=str(data["action"]),
            action_kind=str(data["action_kind"]),
            request_id=str(data["request_id"]),
            inputs=dict(data.get("inputs") or {}),
            outputs=data.get("outputs"),
            status=str(data["status"]),
            timestamp=float(data["timestamp"]),
            signature=str(data["signature"]),
            error=data.get("error"),
        )

    def payload(self) -> dict[str, Any]:
        """Canonical unsigned payload used for signing and verification."""
        return {
            "receipt_id": self.receipt_id,
            "agent_id": self.agent_id,
            "action": self.action,
            "action_kind": self.action_kind,
            "request_id": self.request_id,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "status": self.status,
            "timestamp": self.timestamp,
            "error": self.error,
        }


class AuditReceiptStorage:
    def append(self, receipt: AuditReceiptRecord) -> None:
        raise NotImplementedError

    def list_all(self) -> list[AuditReceiptRecord]:
        raise NotImplementedError


class InMemoryAuditReceiptStorage(AuditReceiptStorage):
    def __init__(self) -> None:
        self._records: list[AuditReceiptRecord] = []

    def append(self, receipt: AuditReceiptRecord) -> None:
        self._records.append(receipt)

    def list_all(self) -> list[AuditReceiptRecord]:
        return list(self._records)


class FileAuditReceiptStorage(AuditReceiptStorage):
    """Append-only JSONL receipt log."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, receipt: AuditReceiptRecord) -> None:
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(receipt.to_dict(), default=str) + "\n")

    def list_all(self) -> list[AuditReceiptRecord]:
        if not self._path.exists():
            return []
        records: list[AuditReceiptRecord] = []
        with self._path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                records.append(AuditReceiptRecord.from_dict(json.loads(line)))
        return records


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def sign_payload(payload: dict[str, Any], signing_key: str) -> str:
    digest = hmac.new(
        signing_key.encode("utf-8"),
        _canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return digest


def verify_receipt(receipt: AuditReceiptRecord, signing_key: str) -> bool:
    expected = sign_payload(receipt.payload(), signing_key)
    return hmac.compare_digest(expected, receipt.signature)


class AuditReceiptEmitter:
    """Emit signed receipts after ledgered tool or task actions."""

    def __init__(
        self,
        *,
        agent_id: str,
        signing_key: str,
        storage: AuditReceiptStorage | None = None,
    ) -> None:
        if not agent_id:
            raise AuditReceiptError("agent_id is required")
        if not signing_key:
            raise AuditReceiptError("signing_key is required")
        self.agent_id = agent_id
        self._signing_key = signing_key
        self._storage = storage if storage is not None else InMemoryAuditReceiptStorage()

    @property
    def storage(self) -> AuditReceiptStorage:
        return self._storage

    def emit_from_tool_entry(self, entry: LedgerEntry) -> AuditReceiptRecord:
        return self._emit(
            action=entry.tool,
            action_kind="tool",
            request_id=entry.request_id,
            inputs={"args": entry.args, "kwargs": entry.kwargs},
            outputs=entry.result,
            status=entry.status,
            error=entry.error,
        )

    def emit_from_task_entry(self, entry: TaskLedgerEntry) -> AuditReceiptRecord:
        return self._emit(
            action=entry.task,
            action_kind="task",
            request_id=entry.request_id,
            inputs={"args": entry.args, "kwargs": entry.kwargs},
            outputs=entry.result,
            status=entry.status,
            error=entry.error,
        )

    def _emit(
        self,
        *,
        action: str,
        action_kind: str,
        request_id: str,
        inputs: dict[str, Any],
        outputs: Any,
        status: str,
        error: str | None,
    ) -> AuditReceiptRecord:
        if status not in ("completed", "failed"):
            raise AuditReceiptError(f"Cannot emit receipt for in-flight status {status!r}")

        receipt_id = f"rcpt-{uuid.uuid4().hex[:16]}"
        timestamp = time.time()
        payload = {
            "receipt_id": receipt_id,
            "agent_id": self.agent_id,
            "action": action,
            "action_kind": action_kind,
            "request_id": request_id,
            "inputs": inputs,
            "outputs": outputs,
            "status": status,
            "timestamp": timestamp,
            "error": error,
        }
        signature = sign_payload(payload, self._signing_key)
        receipt = AuditReceiptRecord(signature=signature, **payload)
        self._storage.append(receipt)
        return receipt


def resolve_signing_key(
    *, signing_key: str | None = None, signing_key_env: str | None = None
) -> str:
    if signing_key:
        return signing_key
    if signing_key_env:
        value = os.environ.get(signing_key_env, "").strip()
        if value:
            return value
        raise AuditReceiptError(f"Environment variable {signing_key_env!r} is not set")
    raise AuditReceiptError("audit receipt requires signing_key or signing_key_env")


__all__ = [
    "AuditReceiptEmitter",
    "AuditReceiptError",
    "AuditReceiptRecord",
    "AuditReceiptStorage",
    "FileAuditReceiptStorage",
    "InMemoryAuditReceiptStorage",
    "resolve_signing_key",
    "sign_payload",
    "verify_receipt",
]
