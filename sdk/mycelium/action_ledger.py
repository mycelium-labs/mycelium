"""ActionLedger: durable action records and idempotency guard."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import os
import socket
import time
import uuid
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from mycelium.session import Session, _session_var
from mycelium.storage._helpers import claim_inflight_outcome, default_try_claim_inflight, with_lease
from mycelium.storage.json_file import LockedJsonDictFile
from mycelium.transition import (
    LEDGER_KWARG_KEYS,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    derive_transition_key_for_call,
    legacy_status_from_terminal,
    resolve_terminal_outcome,
    terminal_from_legacy_status,
)
from mycelium.transition_resolution import (
    TransitionGate,
    hard_block_message,
    resolve_side_effect_gate,
)

if TYPE_CHECKING:
    from mycelium.audit_receipt import AuditReceiptEmitter

P = ParamSpec("P")
R = TypeVar("R")

DEFAULT_LEASE_TTL = 3600.0
DEFAULT_POLL_INTERVAL = 0.05
DEFAULT_POLL_TIMEOUT = 300.0


class LedgerError(Exception):
    """Raised when the action ledger cannot record or verify an action."""


class LedgerPendingError(Exception):
    """Raised when the same request is already in-flight."""


class LedgerPollTimeoutError(LedgerError):
    """Raised when polling for a read-only in-flight transition times out."""


class LedgerHardBlockError(LedgerError):
    """Raised when a side-effecting transition requires manual reconciliation."""


def _ledger_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True)
class LedgerEntry:
    """Immutable record of a single tool invocation."""

    request_id: str
    tool: str
    args: list[Any]
    kwargs: dict[str, Any]
    status: str  # legacy: "in-flight" | "completed" | "failed"
    terminal_outcome: str = TerminalOutcome.IN_FLIGHT.value
    result: Any = None
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    lease_until: float | None = None
    owner: str | None = None
    idempotency_key: str | None = None
    receipt_ref: str | None = None
    side_effect_boundary: str = SideEffectBoundary.NOT_CROSSED.value

    def resolved_terminal_outcome(self, *, now: float | None = None) -> TerminalOutcome:
        return resolve_terminal_outcome(
            self.terminal_outcome,
            lease_until=self.lease_until,
            now=now,
        )

    def is_terminal_completed(self, *, now: float | None = None) -> bool:
        return self.resolved_terminal_outcome(now=now) == TerminalOutcome.COMPLETED

    def is_reclaimable(self, *, now: float | None = None) -> bool:
        outcome = self.resolved_terminal_outcome(now=now)
        return outcome in (
            TerminalOutcome.EXPIRED,
            TerminalOutcome.FAILED_BEFORE_EFFECT,
            TerminalOutcome.FAILED_AFTER_EFFECT,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "tool": self.tool,
            "args": self.args,
            "kwargs": self.kwargs,
            "status": self.status,
            "terminal_outcome": self.terminal_outcome,
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "lease_until": self.lease_until,
            "owner": self.owner,
            "idempotency_key": self.idempotency_key,
            "receipt_ref": self.receipt_ref,
            "side_effect_boundary": self.side_effect_boundary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        status = str(data["status"])
        lease_until = (
            float(data["lease_until"])
            if data.get("lease_until") is not None
            else None
        )
        terminal_raw = data.get("terminal_outcome")
        if terminal_raw is None:
            terminal_outcome = terminal_from_legacy_status(
                status,
                lease_until=lease_until,
            ).value
        else:
            terminal_outcome = str(terminal_raw)
        request_id = str(data["request_id"])
        return cls(
            request_id=request_id,
            tool=str(data["tool"]),
            args=list(data.get("args") or []),
            kwargs=dict(data.get("kwargs") or {}),
            status=status,
            terminal_outcome=terminal_outcome,
            result=data.get("result"),
            error=data.get("error"),
            started_at=float(data.get("started_at", time.time())),
            finished_at=data.get("finished_at"),
            lease_until=lease_until,
            owner=data.get("owner"),
            idempotency_key=data.get("idempotency_key") or request_id,
            receipt_ref=data.get("receipt_ref"),
            side_effect_boundary=str(
                data.get("side_effect_boundary", SideEffectBoundary.NOT_CROSSED.value)
            ),
        )


class LedgerStorage:
    """Backend interface for durable action ledger records."""

    def get(self, request_id: str) -> LedgerEntry | None:
        """Return the entry for request_id, or None if not found."""
        raise NotImplementedError

    def set(self, entry: LedgerEntry) -> None:
        """Persist entry, replacing any existing entry with the same request_id."""
        raise NotImplementedError

    def try_claim_inflight(
        self,
        entry: LedgerEntry,
        *,
        lease_ttl: float = DEFAULT_LEASE_TTL,
    ) -> tuple[str, LedgerEntry | None]:
        """Atomically claim an in-flight entry.

        Returns ``("claimed", None)``, ``("completed", entry)``, or
        ``("in_flight", entry)``. Redis/Postgres backends override with
        atomic primitives; file storage uses an exclusive lock.
        """
        return default_try_claim_inflight(
            self,
            entry,
            lease_ttl=lease_ttl,
        )

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
    """JSON-file-backed storage with ``fcntl`` locking for multi-process safety."""

    def __init__(self, path: str | Path) -> None:
        self._file = LockedJsonDictFile(path)

    def get(self, request_id: str) -> LedgerEntry | None:
        def read(data: dict[str, dict[str, Any]]) -> LedgerEntry | None:
            raw = data.get(request_id)
            if raw is None:
                return None
            return LedgerEntry.from_dict(raw)

        return self._file.read_modify_write_no_save(read)

    def set(self, entry: LedgerEntry) -> None:
        def mutate(data: dict[str, dict[str, Any]]) -> None:
            data[entry.request_id] = entry.to_dict()

        self._file.read_modify_write(mutate)

    def try_claim_inflight(
        self,
        entry: LedgerEntry,
        *,
        lease_ttl: float = DEFAULT_LEASE_TTL,
    ) -> tuple[str, LedgerEntry | None]:
        outcome: list[tuple[str, LedgerEntry | None]] = []

        def mutate(data: dict[str, dict[str, Any]]) -> None:
            raw = data.get(entry.request_id)
            existing = LedgerEntry.from_dict(raw) if raw is not None else None
            now = time.time()
            result = claim_inflight_outcome(existing, now=now)
            if result == "completed":
                outcome.append(("completed", existing))
                return
            if result == "in_flight":
                outcome.append(("in_flight", existing))
                return
            leased = with_lease(entry, now=now, lease_ttl=lease_ttl)
            data[entry.request_id] = leased.to_dict()
            outcome.append(("claimed", None))

        self._file.read_modify_write(mutate)
        return outcome[0]

    def list_all(self) -> list[LedgerEntry]:
        data = self._file.load()
        return [LedgerEntry.from_dict(raw) for raw in data.values()]


class ActionLedger:
    """Durable ledger of tool invocations for idempotency and audit."""

    def __init__(
        self,
        storage: LedgerStorage | None = None,
        *,
        lease_ttl: float = DEFAULT_LEASE_TTL,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        poll_timeout: float | None = DEFAULT_POLL_TIMEOUT,
    ) -> None:
        self._storage = storage if storage is not None else InMemoryLedgerStorage()
        self._lease_ttl = lease_ttl
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout

    # --- public API ---

    def get(self, request_id: str) -> LedgerEntry | None:
        return self._storage.get(request_id)

    def _new_inflight_entry(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        binding: ToolTransitionBinding | None = None,
    ) -> LedgerEntry:
        bound = _bind_args(args, kwargs)
        boundary = (
            binding.side_effect_boundary_default.value
            if binding is not None
            else SideEffectBoundary.NOT_CROSSED.value
        )
        return LedgerEntry(
            request_id=request_id,
            tool=tool,
            args=bound["args"],
            kwargs=bound["kwargs"],
            status=legacy_status_from_terminal(TerminalOutcome.IN_FLIGHT),
            terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
            owner=_ledger_owner(),
            idempotency_key=request_id,
            side_effect_boundary=boundary,
        )

    def claim(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        lease_ttl: float | None = None,
    ) -> LedgerEntry:
        """Claim a request idempotency key before execution.

        Returns the existing completed entry if the request already succeeded.
        Raises LedgerPendingError if the request is currently in-flight.
        """
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        entry = self._new_inflight_entry(request_id, tool, args, kwargs)
        outcome, existing = self._storage.try_claim_inflight(entry, lease_ttl=ttl)
        if outcome == "completed" and existing is not None:
            return existing
        if outcome == "in_flight":
            raise LedgerPendingError(
                f"Tool {tool!r} request {request_id!r} is already in-flight"
            )
        claimed = self.get(request_id)
        return claimed if claimed is not None else entry

    def claim_read_only(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        lease_ttl: float | None = None,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> LedgerEntry:
        """Claim or resolve a read-only tool transition.

        Resolution paths:
        - **Return** cached result when already completed
        - **Poll** while another worker holds a valid in-flight lease
        - **Reclaim** when the in-flight lease is stale (``EXPIRED``)
        - **Retry** after a previous failed attempt
        """
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        interval = self._poll_interval if poll_interval is None else poll_interval
        timeout = self._poll_timeout if poll_timeout is None else poll_timeout
        poll_deadline = time.time() + timeout if timeout is not None else None

        while True:
            entry = self._new_inflight_entry(request_id, tool, args, kwargs)
            outcome, existing = self._storage.try_claim_inflight(entry, lease_ttl=ttl)
            if outcome == "completed" and existing is not None:
                return existing
            if outcome == "claimed":
                claimed = self.get(request_id)
                return claimed if claimed is not None else entry
            if outcome == "in_flight":
                self._poll_read_only(
                    request_id,
                    interval=interval,
                    poll_deadline=poll_deadline,
                )
                continue
            raise LedgerError(
                f"Unexpected claim outcome {outcome!r} for read-only tool {tool!r}"
            )

    def _poll_read_only(
        self,
        request_id: str,
        *,
        interval: float,
        poll_deadline: float | None,
    ) -> None:
        """Wait until a read-only transition leaves the in-flight state."""
        while True:
            if poll_deadline is not None and time.time() >= poll_deadline:
                raise LedgerPollTimeoutError(
                    f"Timed out polling read-only request {request_id!r}"
                )
            time.sleep(interval)
            current = self.get(request_id)
            if current is None:
                return
            outcome = current.resolved_terminal_outcome()
            if outcome == TerminalOutcome.COMPLETED:
                return
            if outcome in (
                TerminalOutcome.FAILED_BEFORE_EFFECT,
                TerminalOutcome.FAILED_AFTER_EFFECT,
            ):
                return
            if outcome == TerminalOutcome.EXPIRED:
                return
            if outcome == TerminalOutcome.IN_FLIGHT:
                continue
            return

    async def claim_read_only_async(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        lease_ttl: float | None = None,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> LedgerEntry:
        """Async variant of :meth:`claim_read_only` for read-only tool polling."""
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        interval = self._poll_interval if poll_interval is None else poll_interval
        timeout = self._poll_timeout if poll_timeout is None else poll_timeout
        poll_deadline = time.time() + timeout if timeout is not None else None

        while True:
            entry = self._new_inflight_entry(request_id, tool, args, kwargs)
            outcome, existing = self._storage.try_claim_inflight(entry, lease_ttl=ttl)
            if outcome == "completed" and existing is not None:
                return existing
            if outcome == "claimed":
                claimed = self.get(request_id)
                return claimed if claimed is not None else entry
            if outcome == "in_flight":
                await self._poll_read_only_async(
                    request_id,
                    interval=interval,
                    poll_deadline=poll_deadline,
                )
                continue
            raise LedgerError(
                f"Unexpected claim outcome {outcome!r} for read-only tool {tool!r}"
            )

    async def _poll_read_only_async(
        self,
        request_id: str,
        *,
        interval: float,
        poll_deadline: float | None,
    ) -> None:
        while True:
            if poll_deadline is not None and time.time() >= poll_deadline:
                raise LedgerPollTimeoutError(
                    f"Timed out polling read-only request {request_id!r}"
                )
            await asyncio.sleep(interval)
            current = self.get(request_id)
            if current is None:
                return
            outcome = current.resolved_terminal_outcome()
            if outcome == TerminalOutcome.COMPLETED:
                return
            if outcome in (
                TerminalOutcome.FAILED_BEFORE_EFFECT,
                TerminalOutcome.FAILED_AFTER_EFFECT,
            ):
                return
            if outcome == TerminalOutcome.EXPIRED:
                return
            if outcome == TerminalOutcome.IN_FLIGHT:
                continue
            return

    def _raise_hard_block(
        self,
        request_id: str,
        tool: str,
        existing: LedgerEntry,
    ) -> None:
        outcome = existing.resolved_terminal_outcome()
        if outcome == TerminalOutcome.EXPIRED:
            existing = self.mark_blocked(
                request_id,
                error="stale in-flight lease; side-effect boundary unknown",
            )
        message = hard_block_message(existing, tool=tool, request_id=request_id)
        raise LedgerHardBlockError(message)

    def claim_side_effecting(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        binding: ToolTransitionBinding,
        *,
        lease_ttl: float | None = None,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> LedgerEntry:
        """Claim or resolve a side-effecting tool transition."""
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        interval = self._poll_interval if poll_interval is None else poll_interval
        timeout = self._poll_timeout if poll_timeout is None else poll_timeout
        poll_deadline = time.time() + timeout if timeout is not None else None

        while True:
            existing = self.get(request_id)
            if existing is not None:
                gate = resolve_side_effect_gate(existing, binding)
                if gate == TransitionGate.RETURN:
                    return existing
                if gate == TransitionGate.HARD_BLOCK:
                    self._raise_hard_block(request_id, tool, existing)
                if gate == TransitionGate.POLL:
                    self._poll_side_effecting(
                        request_id,
                        tool=tool,
                        interval=interval,
                        poll_deadline=poll_deadline,
                    )
                    continue

            entry = self._new_inflight_entry(
                request_id, tool, args, kwargs, binding=binding
            )
            outcome, existing = self._storage.try_claim_inflight(entry, lease_ttl=ttl)
            if outcome == "completed" and existing is not None:
                return existing
            if outcome == "in_flight" and existing is not None:
                gate = resolve_side_effect_gate(existing, binding)
                if gate == TransitionGate.RETURN:
                    return existing
                if gate == TransitionGate.HARD_BLOCK:
                    self._raise_hard_block(request_id, tool, existing)
                self._poll_side_effecting(
                    request_id,
                    tool=tool,
                    interval=interval,
                    poll_deadline=poll_deadline,
                )
                continue
            if outcome == "claimed":
                claimed = self.get(request_id)
                return claimed if claimed is not None else entry
            if existing is not None:
                self._raise_hard_block(request_id, tool, existing)
            raise LedgerError(
                f"Unexpected claim outcome {outcome!r} for side-effecting tool {tool!r}"
            )

    def _poll_side_effecting(
        self,
        request_id: str,
        *,
        tool: str,
        interval: float,
        poll_deadline: float | None,
    ) -> None:
        """Wait for an in-flight side-effecting transition; never auto-reclaim."""
        while True:
            if poll_deadline is not None and time.time() >= poll_deadline:
                current = self.get(request_id)
                if current is not None:
                    self.mark_unknown(
                        request_id,
                        error="timed out polling in-flight side-effecting transition",
                    )
                    self._raise_hard_block(request_id, tool, current)
                raise LedgerPollTimeoutError(
                    f"Timed out polling side-effecting request {request_id!r}"
                )
            time.sleep(interval)
            current = self.get(request_id)
            if current is None:
                return
            outcome = current.resolved_terminal_outcome()
            if outcome == TerminalOutcome.COMPLETED:
                return
            if outcome == TerminalOutcome.EXPIRED:
                self._raise_hard_block(request_id, tool, current)
            if outcome == TerminalOutcome.IN_FLIGHT:
                continue
            if outcome in (
                TerminalOutcome.FAILED_BEFORE_EFFECT,
                TerminalOutcome.FAILED_AFTER_EFFECT,
                TerminalOutcome.BLOCKED,
                TerminalOutcome.UNKNOWN,
            ):
                return

    async def claim_side_effecting_async(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        binding: ToolTransitionBinding,
        *,
        lease_ttl: float | None = None,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> LedgerEntry:
        """Async variant of :meth:`claim_side_effecting`."""
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        interval = self._poll_interval if poll_interval is None else poll_interval
        timeout = self._poll_timeout if poll_timeout is None else poll_timeout
        poll_deadline = time.time() + timeout if timeout is not None else None

        while True:
            existing = self.get(request_id)
            if existing is not None:
                gate = resolve_side_effect_gate(existing, binding)
                if gate == TransitionGate.RETURN:
                    return existing
                if gate == TransitionGate.HARD_BLOCK:
                    self._raise_hard_block(request_id, tool, existing)
                if gate == TransitionGate.POLL:
                    await self._poll_side_effecting_async(
                        request_id,
                        tool=tool,
                        interval=interval,
                        poll_deadline=poll_deadline,
                    )
                    continue

            entry = self._new_inflight_entry(
                request_id, tool, args, kwargs, binding=binding
            )
            outcome, existing = self._storage.try_claim_inflight(entry, lease_ttl=ttl)
            if outcome == "completed" and existing is not None:
                return existing
            if outcome == "in_flight" and existing is not None:
                gate = resolve_side_effect_gate(existing, binding)
                if gate == TransitionGate.RETURN:
                    return existing
                if gate == TransitionGate.HARD_BLOCK:
                    self._raise_hard_block(request_id, tool, existing)
                await self._poll_side_effecting_async(
                    request_id,
                    tool=tool,
                    interval=interval,
                    poll_deadline=poll_deadline,
                )
                continue
            if outcome == "claimed":
                claimed = self.get(request_id)
                return claimed if claimed is not None else entry
            if existing is not None:
                self._raise_hard_block(request_id, tool, existing)
            raise LedgerError(
                f"Unexpected claim outcome {outcome!r} for side-effecting tool {tool!r}"
            )

    async def _poll_side_effecting_async(
        self,
        request_id: str,
        *,
        tool: str,
        interval: float,
        poll_deadline: float | None,
    ) -> None:
        while True:
            if poll_deadline is not None and time.time() >= poll_deadline:
                current = self.get(request_id)
                if current is not None:
                    self.mark_unknown(
                        request_id,
                        error="timed out polling in-flight side-effecting transition",
                    )
                    self._raise_hard_block(request_id, tool, current)
                raise LedgerPollTimeoutError(
                    f"Timed out polling side-effecting request {request_id!r}"
                )
            await asyncio.sleep(interval)
            current = self.get(request_id)
            if current is None:
                return
            outcome = current.resolved_terminal_outcome()
            if outcome == TerminalOutcome.COMPLETED:
                return
            if outcome == TerminalOutcome.EXPIRED:
                self._raise_hard_block(request_id, tool, current)
            if outcome == TerminalOutcome.IN_FLIGHT:
                continue
            if outcome in (
                TerminalOutcome.FAILED_BEFORE_EFFECT,
                TerminalOutcome.FAILED_AFTER_EFFECT,
                TerminalOutcome.BLOCKED,
                TerminalOutcome.UNKNOWN,
            ):
                return

    def complete(self, request_id: str, result: Any) -> LedgerEntry:
        existing = self._storage.get(request_id)
        if existing is None:
            raise LedgerError(f"Cannot complete unknown request {request_id!r}")
        entry = replace(
            existing,
            status=legacy_status_from_terminal(TerminalOutcome.COMPLETED),
            terminal_outcome=TerminalOutcome.COMPLETED.value,
            result=result,
            finished_at=time.time(),
            lease_until=None,
            side_effect_boundary=SideEffectBoundary.CROSSED.value,
        )
        self._storage.set(entry)
        return entry

    def fail(
        self,
        request_id: str,
        error: BaseException,
        *,
        failed_after_effect: bool = False,
    ) -> LedgerEntry:
        existing = self._storage.get(request_id)
        if existing is None:
            raise LedgerError(f"Cannot fail unknown request {request_id!r}")
        terminal = (
            TerminalOutcome.FAILED_AFTER_EFFECT
            if failed_after_effect
            else TerminalOutcome.FAILED_BEFORE_EFFECT
        )
        boundary = (
            SideEffectBoundary.CROSSED.value
            if failed_after_effect
            else existing.side_effect_boundary
        )
        entry = replace(
            existing,
            status=legacy_status_from_terminal(terminal),
            terminal_outcome=terminal.value,
            error=f"{type(error).__name__}: {error}",
            finished_at=time.time(),
            lease_until=None,
            side_effect_boundary=boundary,
        )
        self._storage.set(entry)
        return entry

    def attach_receipt_ref(self, request_id: str, receipt_ref: str) -> LedgerEntry:
        existing = self._storage.get(request_id)
        if existing is None:
            raise LedgerError(f"Cannot attach receipt to unknown request {request_id!r}")
        entry = replace(existing, receipt_ref=receipt_ref)
        self._storage.set(entry)
        return entry

    def mark_blocked(self, request_id: str, *, error: str | None = None) -> LedgerEntry:
        existing = self._storage.get(request_id)
        if existing is None:
            raise LedgerError(f"Cannot block unknown request {request_id!r}")
        entry = replace(
            existing,
            status=legacy_status_from_terminal(TerminalOutcome.BLOCKED),
            terminal_outcome=TerminalOutcome.BLOCKED.value,
            error=error,
            finished_at=time.time(),
            lease_until=None,
        )
        self._storage.set(entry)
        return entry

    def mark_unknown(self, request_id: str, *, error: str | None = None) -> LedgerEntry:
        existing = self._storage.get(request_id)
        if existing is None:
            raise LedgerError(f"Cannot mark unknown request {request_id!r}")
        entry = replace(
            existing,
            status=legacy_status_from_terminal(TerminalOutcome.UNKNOWN),
            terminal_outcome=TerminalOutcome.UNKNOWN.value,
            error=error,
            finished_at=time.time(),
            lease_until=None,
        )
        self._storage.set(entry)
        return entry

    # --- request id derivation ---

    def derive_request_id(
        self,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        transition_binding: ToolTransitionBinding | None = None,
    ) -> str:
        """Determine the request id for a tool invocation.

        When ``transition_binding`` is provided, returns a rich transition key
        derived from execution scope, dispatch id, tool args, and policy fields.

        Legacy priority (no transition binding):
        1. kwargs["request_id"]
        2. kwargs["tool_call_id"]
        3. Session-derived id (run + tool + args hash)
        4. Random UUID (no idempotency, still audited)

        Note: valid repeats within the same Session with identical args will be
        deduplicated unless an explicit request_id is supplied.
        """
        if transition_binding is not None:
            return derive_transition_key_for_call(
                tool, args, kwargs, transition_binding
            )

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
    return {k: v for k, v in kwargs.items() if k not in LEDGER_KWARG_KEYS}


def _emit_tool_receipt(
    audit_emitter: AuditReceiptEmitter | None,
    ledger: ActionLedger,
    request_id: str,
) -> None:
    if audit_emitter is None:
        return
    entry = ledger.get(request_id)
    if entry is None:
        return
    outcome = entry.resolved_terminal_outcome()
    if outcome not in (
        TerminalOutcome.COMPLETED,
        TerminalOutcome.FAILED_BEFORE_EFFECT,
        TerminalOutcome.FAILED_AFTER_EFFECT,
    ):
        return
    receipt = audit_emitter.emit_from_tool_entry(entry)
    ledger.attach_receipt_ref(request_id, receipt.receipt_id)


def _is_read_only_binding(
    transition_binding: ToolTransitionBinding | None,
) -> bool:
    return (
        transition_binding is not None
        and transition_binding.side_effect_class == SideEffectClass.READ_ONLY
    )


def _claim_for_transition(
    ledger: ActionLedger,
    request_id: str,
    tool_name: str,
    args: tuple[Any, ...],
    clean_kwargs: dict[str, Any],
    transition_binding: ToolTransitionBinding | None,
) -> LedgerEntry:
    if _is_read_only_binding(transition_binding):
        return ledger.claim_read_only(
            request_id, tool_name, args, clean_kwargs
        )
    if transition_binding is not None:
        return ledger.claim_side_effecting(
            request_id,
            tool_name,
            args,
            clean_kwargs,
            transition_binding,
        )
    return ledger.claim(request_id, tool_name, args, clean_kwargs)


async def _claim_for_transition_async(
    ledger: ActionLedger,
    request_id: str,
    tool_name: str,
    args: tuple[Any, ...],
    clean_kwargs: dict[str, Any],
    transition_binding: ToolTransitionBinding | None,
) -> LedgerEntry:
    if _is_read_only_binding(transition_binding):
        return await ledger.claim_read_only_async(
            request_id, tool_name, args, clean_kwargs
        )
    if transition_binding is not None:
        return await ledger.claim_side_effecting_async(
            request_id,
            tool_name,
            args,
            clean_kwargs,
            transition_binding,
        )
    return ledger.claim(request_id, tool_name, args, clean_kwargs)


def _run_ledgered(
    func: Callable[P, R],
    tool_name: str,
    ledger: ActionLedger,
    args: P.args,
    kwargs: P.kwargs,
    audit_emitter: AuditReceiptEmitter | None = None,
    transition_binding: ToolTransitionBinding | None = None,
) -> R:
    request_id = ledger.derive_request_id(
        tool_name,
        args,
        kwargs,
        transition_binding=transition_binding,
    )
    clean_kwargs = _drop_ledger_keys(kwargs)
    existing = _claim_for_transition(
        ledger,
        request_id,
        tool_name,
        args,
        clean_kwargs,
        transition_binding,
    )
    if existing.is_terminal_completed():
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


async def _run_ledgered_async(
    func: Callable[P, Awaitable[R]],
    tool_name: str,
    ledger: ActionLedger,
    args: P.args,
    kwargs: P.kwargs,
    audit_emitter: AuditReceiptEmitter | None = None,
    transition_binding: ToolTransitionBinding | None = None,
) -> R:
    request_id = ledger.derive_request_id(
        tool_name,
        args,
        kwargs,
        transition_binding=transition_binding,
    )
    clean_kwargs = _drop_ledger_keys(kwargs)
    existing = await _claim_for_transition_async(
        ledger,
        request_id,
        tool_name,
        args,
        clean_kwargs,
        transition_binding,
    )
    if existing.is_terminal_completed():
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
    transition_binding: ToolTransitionBinding | None = None,
    *,
    lease_ttl: float | None = None,
    poll_interval: float | None = None,
    poll_timeout: float | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that records async tool invocations in an ActionLedger."""

    ledger_kwargs: dict[str, float | None] = {}
    if lease_ttl is not None:
        ledger_kwargs["lease_ttl"] = lease_ttl
    if poll_interval is not None:
        ledger_kwargs["poll_interval"] = poll_interval
    if poll_timeout is not None:
        ledger_kwargs["poll_timeout"] = poll_timeout
    action_ledger = ActionLedger(storage=storage, **ledger_kwargs)

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        tool_name = func.__name__

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return await _run_ledgered_async(
                func,
                tool_name,
                action_ledger,
                args,
                kwargs,
                audit_emitter,
                transition_binding,
            )

        _mark_ledgered(wrapper, action_ledger)
        return wrapper

    return decorator


def ledger_sync(
    storage: LedgerStorage | None = None,
    audit_emitter: AuditReceiptEmitter | None = None,
    transition_binding: ToolTransitionBinding | None = None,
    *,
    lease_ttl: float | None = None,
    poll_interval: float | None = None,
    poll_timeout: float | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that records sync tool invocations in an ActionLedger."""

    ledger_kwargs: dict[str, float | None] = {}
    if lease_ttl is not None:
        ledger_kwargs["lease_ttl"] = lease_ttl
    if poll_interval is not None:
        ledger_kwargs["poll_interval"] = poll_interval
    if poll_timeout is not None:
        ledger_kwargs["poll_timeout"] = poll_timeout
    action_ledger = ActionLedger(storage=storage, **ledger_kwargs)

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        tool_name = func.__name__

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            return _run_ledgered(
                func,
                tool_name,
                action_ledger,
                args,
                kwargs,
                audit_emitter,
                transition_binding,
            )

        _mark_ledgered(wrapper, action_ledger)
        return wrapper

    return decorator


def get_ledger(func: Callable[..., Any]) -> ActionLedger | None:
    """Return the ActionLedger attached to a wrapped function, if any."""
    return getattr(func, "_mycelium_ledger_instance", None)


__all__ = [
    "ActionLedger",
    "DEFAULT_LEASE_TTL",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_POLL_TIMEOUT",
    "FileLedgerStorage",
    "InMemoryLedgerStorage",
    "LedgerEntry",
    "LedgerError",
    "LedgerHardBlockError",
    "LedgerPendingError",
    "LedgerPollTimeoutError",
    "LedgerStorage",
    "TerminalOutcome",
    "get_ledger",
    "ledger",
    "ledger_sync",
]
