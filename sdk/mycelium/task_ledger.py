"""TaskLedger — AF-002 task-level durable records and idempotency guard."""

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

if TYPE_CHECKING:
    from mycelium.audit_receipt import AuditReceiptEmitter

P = ParamSpec("P")
R = TypeVar("R")


class TaskLedgerError(Exception):
    """Raised when the task ledger cannot record or verify a task."""


class TaskLedgerPendingError(Exception):
    """Raised when the same task is already in-flight."""


@dataclass(frozen=True)
class TaskLedgerEntry:
    """Immutable record of a single task execution."""

    request_id: str
    task: str
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
            "task": self.task,
            "args": self.args,
            "kwargs": self.kwargs,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskLedgerEntry:
        return cls(**data)


class TaskLedgerStorage:
    """Backend interface for durable task ledger entries."""

    def get(self, request_id: str) -> TaskLedgerEntry | None:
        """Return the entry for request_id, or None if not found."""
        raise NotImplementedError

    def set(self, entry: TaskLedgerEntry) -> None:
        """Persist entry, replacing any existing entry with the same request_id."""
        raise NotImplementedError

    def list_all(self) -> list[TaskLedgerEntry]:
        """Return all entries. Intended for debugging/auditing only."""
        raise NotImplementedError


class TaskInMemoryLedgerStorage(TaskLedgerStorage):
    """Default in-memory storage. Survives within the process only."""

    def __init__(self) -> None:
        self._entries: dict[str, TaskLedgerEntry] = {}

    def get(self, request_id: str) -> TaskLedgerEntry | None:
        return self._entries.get(request_id)

    def set(self, entry: TaskLedgerEntry) -> None:
        self._entries[entry.request_id] = entry

    def list_all(self) -> list[TaskLedgerEntry]:
        return list(self._entries.values())


class TaskFileLedgerStorage(TaskLedgerStorage):
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

    def get(self, request_id: str) -> TaskLedgerEntry | None:
        data = self._load()
        raw = data.get(request_id)
        if raw is None:
            return None
        return TaskLedgerEntry.from_dict(raw)

    def set(self, entry: TaskLedgerEntry) -> None:
        data = self._load()
        data[entry.request_id] = entry.to_dict()
        self._save(data)

    def list_all(self) -> list[TaskLedgerEntry]:
        return [TaskLedgerEntry.from_dict(raw) for raw in self._load().values()]


class TaskLedger:
    """Durable ledger of task executions for idempotency and audit."""

    def __init__(
        self,
        storage: TaskLedgerStorage | None = None,
        *,
        id_from: list[str] | None = None,
    ) -> None:
        self._storage = storage if storage is not None else TaskInMemoryLedgerStorage()
        self._id_from = id_from or []

    # --- public API ---

    def get(self, request_id: str) -> TaskLedgerEntry | None:
        return self._storage.get(request_id)

    def claim(
        self,
        request_id: str,
        task: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> TaskLedgerEntry:
        """Claim a task idempotency key before execution.

        Returns the existing completed entry if the task already succeeded.
        Raises TaskLedgerPendingError if the task is currently in-flight.
        """
        existing = self._storage.get(request_id)
        if existing is not None:
            if existing.status == "completed":
                return existing
            if existing.status == "in-flight":
                raise TaskLedgerPendingError(
                    f"Task {task!r} request {request_id!r} is already in-flight"
                )
            # failed: allow retry by falling through to re-claim

        bound = _bind_args(args, kwargs)
        entry = TaskLedgerEntry(
            request_id=request_id,
            task=task,
            args=bound["args"],
            kwargs=bound["kwargs"],
            status="in-flight",
        )
        self._storage.set(entry)
        return entry

    def complete(self, request_id: str, result: Any) -> TaskLedgerEntry:
        existing = self._storage.get(request_id)
        if existing is None:
            raise TaskLedgerError(f"Cannot complete unknown request {request_id!r}")
        entry = TaskLedgerEntry(
            request_id=existing.request_id,
            task=existing.task,
            args=existing.args,
            kwargs=existing.kwargs,
            status="completed",
            result=result,
            started_at=existing.started_at,
            finished_at=time.time(),
        )
        self._storage.set(entry)
        return entry

    def fail(self, request_id: str, error: BaseException) -> TaskLedgerEntry:
        existing = self._storage.get(request_id)
        if existing is None:
            raise TaskLedgerError(f"Cannot fail unknown request {request_id!r}")
        entry = TaskLedgerEntry(
            request_id=existing.request_id,
            task=existing.task,
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
        task: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> str:
        """Determine the request id for a task execution.

        Priority:
        1. kwargs["task_id"]
        2. kwargs["run_id"]
        3. Configured id_from fields
        4. Stable hash of task name + kwargs
        5. Random UUID (no idempotency, still audited)
        """
        if "task_id" in kwargs:
            return str(kwargs["task_id"])
        if "run_id" in kwargs:
            return f"{task}:{kwargs['run_id']}"
        if self._id_from:
            key_parts = []
            for field in self._id_from:
                if field in kwargs:
                    key_parts.append(f"{field}={kwargs[field]}")
            if key_parts:
                return f"{task}:" + ":".join(key_parts)

        args_hash = self._hash_args(args, kwargs)
        derived = f"{task}:{args_hash}"
        if derived:
            return derived

        warnings.warn(
            f"Task {task!r} has no task_id, run_id, id_from, or hashable args; "
            "TaskLedger cannot deduplicate this task. A random UUID will be used.",
            stacklevel=4,
        )
        return f"no-task-id:{task}:{uuid.uuid4()}"

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


def _drop_task_keys(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Remove Mycelium bookkeeping keys before calling the actual task."""
    return {k: v for k, v in kwargs.items() if k not in ("task_id", "run_id")}


def _emit_task_receipt(
    audit_emitter: AuditReceiptEmitter | None,
    ledger: TaskLedger,
    request_id: str,
) -> None:
    if audit_emitter is None:
        return
    entry = ledger.get(request_id)
    if entry is not None and entry.status in ("completed", "failed"):
        audit_emitter.emit_from_task_entry(entry)


def _run_task_ledgered(
    func: Callable[P, R],
    task_name: str,
    ledger: TaskLedger,
    args: P.args,
    kwargs: P.kwargs,
    audit_emitter: AuditReceiptEmitter | None = None,
) -> R:
    request_id = ledger.derive_request_id(task_name, args, kwargs)
    clean_kwargs = _drop_task_keys(kwargs)
    existing = ledger.claim(request_id, task_name, args, clean_kwargs)
    if existing.status == "completed":
        return existing.result

    try:
        result = func(*args, **clean_kwargs)
    except Exception as exc:
        ledger.fail(request_id, exc)
        _emit_task_receipt(audit_emitter, ledger, request_id)
        raise

    ledger.complete(request_id, result)
    _emit_task_receipt(audit_emitter, ledger, request_id)
    return result


async def _run_task_ledgered_async(
    func: Callable[P, Awaitable[R]],
    task_name: str,
    ledger: TaskLedger,
    args: P.args,
    kwargs: P.kwargs,
    audit_emitter: AuditReceiptEmitter | None = None,
) -> R:
    request_id = ledger.derive_request_id(task_name, args, kwargs)
    clean_kwargs = _drop_task_keys(kwargs)
    existing = ledger.claim(request_id, task_name, args, clean_kwargs)
    if existing.status == "completed":
        return existing.result

    try:
        result = await func(*args, **clean_kwargs)
    except Exception as exc:
        ledger.fail(request_id, exc)
        _emit_task_receipt(audit_emitter, ledger, request_id)
        raise

    ledger.complete(request_id, result)
    _emit_task_receipt(audit_emitter, ledger, request_id)
    return result


def _mark_task_ledgered(wrapper: Callable[..., Any], ledger: TaskLedger) -> None:
    wrapper._mycelium_task_ledger = True  # type: ignore[attr-defined]
    wrapper._mycelium_task_ledger_instance = ledger  # type: ignore[attr-defined]


def task_ledger(
    storage: TaskLedgerStorage | None = None,
    *,
    id_from: list[str] | None = None,
    audit_emitter: AuditReceiptEmitter | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that records async task executions in a TaskLedger."""

    task_ledger_instance = TaskLedger(storage=storage, id_from=id_from)

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        task_name = func.__name__

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return await _run_task_ledgered_async(
                func, task_name, task_ledger_instance, args, kwargs, audit_emitter
            )

        _mark_task_ledgered(wrapper, task_ledger_instance)
        return wrapper

    return decorator


def task_ledger_sync(
    storage: TaskLedgerStorage | None = None,
    *,
    id_from: list[str] | None = None,
    audit_emitter: AuditReceiptEmitter | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that records sync task executions in a TaskLedger."""

    task_ledger_instance = TaskLedger(storage=storage, id_from=id_from)

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        task_name = func.__name__

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return _run_task_ledgered(
                func, task_name, task_ledger_instance, args, kwargs, audit_emitter
            )

        _mark_task_ledgered(wrapper, task_ledger_instance)
        return wrapper

    return decorator


def get_task_ledger(func: Callable[..., Any]) -> TaskLedger | None:
    """Return the TaskLedger attached to a wrapped function, if any."""
    return getattr(func, "_mycelium_task_ledger_instance", None)


__all__ = [
    "TaskFileLedgerStorage",
    "TaskInMemoryLedgerStorage",
    "TaskLedger",
    "TaskLedgerEntry",
    "TaskLedgerError",
    "TaskLedgerPendingError",
    "TaskLedgerStorage",
    "get_task_ledger",
    "task_ledger",
    "task_ledger_sync",
]
