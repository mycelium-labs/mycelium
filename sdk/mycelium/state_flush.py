"""StateFlush — AF-002 partial state persistence on cancel, disconnect, or error."""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mycelium.session import Session

FlushReason = Literal["cancel", "disconnect", "error"]
SnapshotStatus = Literal["in-progress", "completed", "aborted", "error"]


class StateFlushError(Exception):
    """Raised when state cannot be flushed or loaded."""


@dataclass(frozen=True)
class StateSnapshot:
    """Durable record of agent run state at a point in time."""

    run_id: str
    status: SnapshotStatus
    state: dict[str, Any]
    updated_at: float = field(default_factory=time.time)
    reason: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "state": self.state,
            "updated_at": self.updated_at,
            "reason": self.reason,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateSnapshot:
        return cls(
            run_id=str(data["run_id"]),
            status=data["status"],
            state=dict(data.get("state") or {}),
            updated_at=float(data.get("updated_at", time.time())),
            reason=data.get("reason"),
            error=data.get("error"),
        )


class StateFlushStorage:
    """Backend interface for flushed run state."""

    def get(self, run_id: str) -> StateSnapshot | None:
        raise NotImplementedError

    def set(self, snapshot: StateSnapshot) -> None:
        raise NotImplementedError


class InMemoryStateFlushStorage(StateFlushStorage):
    def __init__(self) -> None:
        self._snapshots: dict[str, StateSnapshot] = {}

    def get(self, run_id: str) -> StateSnapshot | None:
        return self._snapshots.get(run_id)

    def set(self, snapshot: StateSnapshot) -> None:
        self._snapshots[snapshot.run_id] = snapshot


class FileStateFlushStorage(StateFlushStorage):
    """JSON-file-backed storage keyed by run_id."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def _save(self, data: dict[str, dict[str, Any]]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, default=str)
        os.replace(tmp, self._path)

    def get(self, run_id: str) -> StateSnapshot | None:
        raw = self._load().get(run_id)
        if raw is None:
            return None
        return StateSnapshot.from_dict(raw)

    def set(self, snapshot: StateSnapshot) -> None:
        data = self._load()
        data[snapshot.run_id] = snapshot.to_dict()
        self._save(data)


class _FlushRun:
    """Handle for recording and flushing state within a single run."""

    def __init__(self, flush: StateFlush, run_id: str) -> None:
        self._flush = flush
        self._run_id = run_id
        self._state: dict[str, Any] = {}

    @property
    def run_id(self) -> str:
        return self._run_id

    def record(self, patch: dict[str, Any]) -> None:
        """Merge patch into the in-memory run state."""
        self._state.update(patch)

    @property
    def state(self) -> dict[str, Any]:
        return dict(self._state)

    def flush(self, status: SnapshotStatus, *, reason: str | None = None, error: str | None = None) -> StateSnapshot:
        snapshot = StateSnapshot(
            run_id=self._run_id,
            status=status,
            state=dict(self._state),
            reason=reason,
            error=error,
        )
        self._flush._storage.set(snapshot)
        return snapshot

    def complete(self) -> StateSnapshot:
        return self.flush("completed")

    def disconnect(self) -> StateSnapshot:
        if not self._flush._should_flush("disconnect"):
            return self.flush("completed")
        return self.flush("aborted", reason="disconnect")


_active_flush_run: ContextVar[_FlushRun | None] = ContextVar(
    "mycelium_active_flush_run", default=None
)


def get_active_flush_run() -> _FlushRun | None:
    return _active_flush_run.get()


class StateFlush:
    """Persist in-progress agent state when a run aborts before checkpoint."""

    def __init__(
        self,
        storage: StateFlushStorage | None = None,
        *,
        flush_on: list[FlushReason] | None = None,
        flush_on_complete: bool = True,
    ) -> None:
        self._storage = storage if storage is not None else InMemoryStateFlushStorage()
        self._flush_on = set(flush_on or ["cancel", "disconnect", "error"])
        self._flush_on_complete = flush_on_complete

    def load(self, run_id: str) -> StateSnapshot | None:
        return self._storage.get(run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        """Return flushed state for a run, or raise if nothing was persisted."""
        snapshot = self.load(run_id)
        if snapshot is None:
            raise StateFlushError(f"No flushed state found for run {run_id!r}")
        return dict(snapshot.state)

    def run(
        self,
        run_id: str,
        *,
        use_session: bool = True,
    ) -> AbstractContextManager[_FlushRun]:
        return _StateFlushContext(self, run_id, use_session=use_session)

    def _should_flush(self, reason: FlushReason) -> bool:
        return reason in self._flush_on


class _StateFlushContext(AbstractContextManager[_FlushRun]):
    def __init__(self, flush: StateFlush, run_id: str, *, use_session: bool) -> None:
        self._flush = flush
        self._run_id = run_id
        self._use_session = use_session
        self._session: Session | None = None
        self._run: _FlushRun | None = None
        self._token: Any = None

    def __enter__(self) -> _FlushRun:
        if self._use_session:
            self._session = Session()
            self._session.__enter__()
        self._run = _FlushRun(self._flush, self._run_id)
        self._token = _active_flush_run.set(self._run)
        return self._run

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, *_: Any) -> bool:
        try:
            if self._run is not None:
                if exc is None:
                    if self._flush._flush_on_complete:
                        self._run.complete()
                elif isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt)):
                    if self._flush._should_flush("cancel"):
                        self._run.flush("aborted", reason="cancel", error=str(exc))
                else:
                    if self._flush._should_flush("error"):
                        self._run.flush("error", reason="error", error=f"{exc_type.__name__}: {exc}")
        finally:
            if self._session is not None:
                self._session.__exit__(exc_type, exc, None)
            if hasattr(self, "_token") and self._token is not None:
                _active_flush_run.reset(self._token)
        return False


__all__ = [
    "FileStateFlushStorage",
    "FlushReason",
    "InMemoryStateFlushStorage",
    "SnapshotStatus",
    "StateFlush",
    "StateFlushError",
    "StateFlushStorage",
    "StateSnapshot",
    "get_active_flush_run",
]
