"""ActionLedger — AF-002 durable action records and idempotency guard."""

from __future__ import annotations

import functools
import hashlib
import json
import os
import time
import uuid
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from mycelium.session import Session, _session_var

if TYPE_CHECKING:
    from mycelium.audit_receipt import AuditReceiptEmitter

P = ParamSpec("P")
R = TypeVar("R")


class LedgerError(Exception):
    """Raised when the action ledger cannot record or verify an action."""


class LedgerPendingError(Exception):
    """Raised when the same request is already in-flight."""


@dataclass(frozen=True)
class LedgerEntry:
    """Immutable record of a single tool invocation."""

    request_id: str
    tool: str
    args: list[Any]
    kwargs: dict[str, Any]
    status: str  # "in-flight" | "completed" | "failed"
    result: Any = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool": self.tool,
            "args": self.args,
            "kwargs": self.kwargs,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        return cls(**data)


class LedgerStorage:
    """Backend interface for durable action ledger records."""

    def get(self, request_id: str) -> LedgerEntry | None:
        """Return the entry for request_id, or None if not found."""
        raise NotImplementedError

    def set(self, entry: LedgerEntry) -> None:
        """Persist entry, replacing any existing entry with the same request_id."""
        raise NotImplementedError

    def list_all(self) -> list[LedgerEntry]:
        """Return all entries. Intended for debugging/auditing only."""
        raise NotImplementedError


class InMemoryLedgerStorage(LedgerStorage):
    """Default in-memory storage. Survives within the process only."""

    def __init__(self) -> None:
        self._entries: dict[str, LedgerEntry] = {}

    def get(self, request_id: str) -> LedgerEntry | None:
        return self._entries.get(request_id)

    def set(self, entry: LedgerEntry) -> None:
        self._entries[entry.request_id] = entry

    def list_all(self) -> list[LedgerEntry]:
        return list(self._entries.values())


class FileLedgerStorage(LedgerStorage):
    """JSON-file-backed storage. Best-effort concurrency; prefer a real backend at scale."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, self._path)

    def get(self, request_id: str) -> LedgerEntry | None:
        data = self._load()
        raw = data.get(request_id)
        if raw is None:
            return None
        return LedgerEntry.from_dict(raw)

    def set(self, entry: LedgerEntry) -> None:
        data = self._load()
        data[entry.request_id] = entry.to_dict()
        self._save(data)

    def list_all(self) -> list[LedgerEntry]:
        return [LedgerEntry.from_dict(raw) for raw in self._load().values()]


class ActionLedger:
    """Durable ledger of tool invocations for idempotency and audit."""

    def __init__(self, storage: LedgerStorage | None = None) -> None:
        self._storage = storage if storage is not None else InMemoryLedgerStorage()

    # --- public API ---

    def get(self, request_id: str) -> LedgerEntry | None:
        return self._storage.get(request_id)

    def claim(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> LedgerEntry:
        """Claim a request idempotency key before execution.

        Returns the existing completed entry if the request already succeeded.
        Raises LedgerPendingError if the request is currently in-flight.
        """
        existing = self._storage.get(request_id)
        if existing is not None:
            if existing.status == "completed":
                return existing
            if existing.status == "in-flight":
                raise LedgerPendingError(
                    f"Tool {tool!r} request {request_id!r} is already in-flight"
                )
            # failed: allow retry by falling through to re-claim

        bound = _bind_args(args, kwargs)
        entry = LedgerEntry(
            request_id=request_id,
            tool=tool,
            args=bound["args"],
            kwargs=bound["kwargs"],
            status="in-flight",
        )
        self._storage.set(entry)
        return entry

    def complete(self, request_id: str, result: Any) -> LedgerEntry:
        existing = self._storage.get(request_id)
        if existing is None:
            raise LedgerError(f"Cannot complete unknown request {request_id!r}")
        entry = LedgerEntry(
            request_id=existing.request_id,
            tool=existing.tool,
            args=existing.args,
            kwargs=existing.kwargs,
            status="completed",
            result=result,
            started_at=existing.started_at,
            finished_at=time.time(),
        )
        self._storage.set(entry)
        return entry

    def fail(self, request_id: str, error: BaseException) -> LedgerEntry:
        existing = self._storage.get(request_id)
        if existing is None:
            raise LedgerError(f"Cannot fail unknown request {request_id!r}")
        entry = LedgerEntry(
            request_id=existing.request_id,
            tool=existing.tool,
            args=existing.args,
            kwargs=existing.kwargs,
            status="failed",
            error=f"{type(error).__name__}: {error}",
            started_at=existing.started_at,
            finished_at=time.time(),
        )
        self._storage.set(entry)
        return entry

    # --- request id derivation ---

    def derive_request_id(
        self,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> str:
        """Determine the request id for a tool invocation.

        Priority:
        1. kwargs["request_id"]
        2. kwargs["tool_call_id"]
        3. Session-derived id (run + tool + args hash)
        4. Random UUID (no idempotency, still audited)

        Note: valid repeats within the same Session with identical args will be
        deduplicated unless an explicit request_id is supplied.
        """
        if "request_id" in kwargs:
            return str(kwargs["request_id"])
        if "tool_call_id" in kwargs:
            return str(kwargs["tool_call_id"])

        session = _session_var.get()
        if session is not None:
            return self._session_request_id(session, tool, args, kwargs)

        warnings.warn(
            f"Tool {tool!r} has no request_id, tool_call_id, or Session; "
            "ActionLedger cannot deduplicate this call. A random UUID will be used.",
            stacklevel=4,
        )
        return f"no-session:{tool}:{uuid.uuid4()}"

    def _session_request_id(
        self, session: Session, tool: str, args: tuple[Any, ...], kwargs: dict[str, Any]
    ) -> str:
        # Stable within the process for the lifetime of the Session object.
        run_key = f"run-{id(session)}"
        args_hash = self._hash_args(args, kwargs)
        return f"{run_key}:{tool}:{args_hash}"

    @staticmethod
    def _hash_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        payload = json.dumps(
            {"args": args, "kwargs": kwargs},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _bind_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """Store a serializable snapshot of the call arguments."""
    return {
        "args": list(args),
        "kwargs": dict(kwargs),
    }


def _drop_ledger_keys(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Remove Mycelium bookkeeping keys before calling the actual tool."""
    return {k: v for k, v in kwargs.items() if k not in ("request_id", "tool_call_id")}


def _emit_tool_receipt(
    audit_emitter: AuditReceiptEmitter | None,
    ledger: ActionLedger,
    request_id: str,
) -> None:
    if audit_emitter is None:
        return
    entry = ledger.get(request_id)
    if entry is not None and entry.status in ("completed", "failed"):
        audit_emitter.emit_from_tool_entry(entry)


def _run_ledgered[**P, R](
    func: Callable[P, R],
    tool_name: str,
    ledger: ActionLedger,
    args: P.args,
    kwargs: P.kwargs,
    audit_emitter: AuditReceiptEmitter | None = None,
) -> R:
    request_id = ledger.derive_request_id(tool_name, args, kwargs)
    clean_kwargs = _drop_ledger_keys(kwargs)
    existing = ledger.claim(request_id, tool_name, args, clean_kwargs)
    if existing.status == "completed":
        return existing.result

    try:
        result = func(*args, **clean_kwargs)
    except Exception as exc:
        ledger.fail(request_id, exc)
        _emit_tool_receipt(audit_emitter, ledger, request_id)
        raise

    ledger.complete(request_id, result)
    _emit_tool_receipt(audit_emitter, ledger, request_id)
    return result


async def _run_ledgered_async[**P, R](
    func: Callable[P, Awaitable[R]],
    tool_name: str,
    ledger: ActionLedger,
    args: P.args,
    kwargs: P.kwargs,
    audit_emitter: AuditReceiptEmitter | None = None,
) -> R:
    request_id = ledger.derive_request_id(tool_name, args, kwargs)
    clean_kwargs = _drop_ledger_keys(kwargs)
    existing = ledger.claim(request_id, tool_name, args, clean_kwargs)
    if existing.status == "completed":
        return existing.result

    try:
        result = await func(*args, **clean_kwargs)
    except Exception as exc:
        ledger.fail(request_id, exc)
        _emit_tool_receipt(audit_emitter, ledger, request_id)
        raise

    ledger.complete(request_id, result)
    _emit_tool_receipt(audit_emitter, ledger, request_id)
    return result


def _mark_ledgered(wrapper: Callable[..., Any], ledger: ActionLedger) -> None:
    wrapper._mycelium_ledger = True  # type: ignore[attr-defined]
    wrapper._mycelium_ledger_instance = ledger  # type: ignore[attr-defined]


def ledger(
    storage: LedgerStorage | None = None,
    audit_emitter: AuditReceiptEmitter | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that records async tool invocations in an ActionLedger."""

    action_ledger = ActionLedger(storage=storage)

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        tool_name = func.__name__

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return await _run_ledgered_async(
                func, tool_name, action_ledger, args, kwargs, audit_emitter
            )

        _mark_ledgered(wrapper, action_ledger)
        return wrapper

    return decorator


def ledger_sync(
    storage: LedgerStorage | None = None,
    audit_emitter: AuditReceiptEmitter | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that records sync tool invocations in an ActionLedger."""

    action_ledger = ActionLedger(storage=storage)

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        tool_name = func.__name__

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return _run_ledgered(
                func, tool_name, action_ledger, args, kwargs, audit_emitter
            )

        _mark_ledgered(wrapper, action_ledger)
        return wrapper

    return decorator


def get_ledger(func: Callable[..., Any]) -> ActionLedger | None:
    """Return the ActionLedger attached to a wrapped function, if any."""
    return getattr(func, "_mycelium_ledger_instance", None)


__all__ = [
    "ActionLedger",
    "FileLedgerStorage",
    "InMemoryLedgerStorage",
    "LedgerEntry",
    "LedgerError",
    "LedgerPendingError",
    "LedgerStorage",
    "get_ledger",
    "ledger",
    "ledger_sync",
]
