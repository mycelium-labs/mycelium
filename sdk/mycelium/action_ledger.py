"""ActionLedger: durable action records and idempotency guard."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import socket
import threading
import time
import uuid
import warnings
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from mycelium.reconcile import Reconciler, ReconcileResult, ReconcileStatus
from mycelium.session import Session, _session_var
from mycelium.storage._helpers import claim_inflight_outcome, default_try_claim_inflight, with_lease
from mycelium.storage.json_file import LockedJsonDictFile
from mycelium.transition import (
    LEDGER_KWARG_KEYS,
    LeaseValidity,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    derive_transition_key_for_call,
    extract_provider_idempotency_key,
    get_active_dispatch_id,
    has_worker_death_evidence,
    legacy_status_from_terminal,
    resolve_lease_validity,
    resolve_terminal_outcome,
    terminal_from_legacy_status,
)
from mycelium.transition_resolution import (
    TransitionGate,
    hard_block_message,
    repair_transition_fields,
    resolve_read_only_gate,
    resolve_side_effect_gate,
    soft_block_message,
    transition_needs_repair,
)

if TYPE_CHECKING:
    from mycelium.audit_receipt import AuditReceiptEmitter
    from mycelium.outcome_emit import OutcomeEmitter

P = ParamSpec("P")
R = TypeVar("R")

DEFAULT_LEASE_TTL = 3600.0
DEFAULT_POLL_INTERVAL = 0.05
DEFAULT_POLL_TIMEOUT = 300.0
# Renew at 1/3 of lease TTL so a still-running owner stays HELD before peers see EXPIRED.
DEFAULT_LEASE_RENEW_RATIO = 1.0 / 3.0
MIN_LEASE_RENEW_INTERVAL = 0.01
# Default grace window for worker-death evidence: 2x the lease TTL.
DEFAULT_PRESUMED_DEAD_AFTER_RATIO = 2.0

_logger = logging.getLogger(__name__)


class LedgerError(Exception):
    """Raised when the action ledger cannot record or verify an action."""


class LedgerPendingError(Exception):
    """Raised when the same request is already in-flight."""


class LedgerPollTimeoutError(LedgerError):
    """Raised when polling for a read-only in-flight transition times out."""


class LedgerHardBlockError(LedgerError):
    """Raised when a side-effecting transition requires manual reconciliation."""


class LedgerSoftBlockError(LedgerError):
    """Raised when a reversible (read-only) transition is deferred.

    Signals an ambiguous ``UNKNOWN`` / ``BLOCKED`` outcome on a read-only tool.
    Unlike :class:`LedgerHardBlockError`, re-running the tool is safe, so this
    is a *deferral* the caller may retry later rather than a terminal stop. Only
    raised when the ledger is configured with ``defer_read_only_unknown=True``.
    """


class LedgerReleaseRefusedError(LedgerError):
    """Raised when an operator release is rejected (fail-closed).

    Covers unknown request ids, releasing a ``COMPLETED`` transition, and
    releasing an ``IN_FLIGHT`` transition whose lease is still held (a worker
    may be alive).
    """


class LedgerAlreadyResolvedError(LedgerError):
    """Raised when releasing a transition that already has an operator resolution.

    Release is one-shot: a recorded human verification is never overwritten.
    """


class LedgerOutcomeAlreadySetError(LedgerError):
    """Raised when a terminal-outcome write is refused because the transition
    already has a terminal outcome (the outcome is one-shot).  Analogous to
    HTTP 409 Conflict: a stale worker or late duplicate tried to write an
    outcome after the transition was already resolved elsewhere.

    Pre-upgrade behaviour silently overwrote the true outcome.  This exception
    is the new fail-closed guard.
    """


class LedgerWorkerAliveError(LedgerError):
    """Raised when a worker-death assertion is refused because the worker appears alive.

    Covers ``mark_worker_dead`` on an entry whose ``last_heartbeat_at`` is
    within the grace window, and ``release()`` of an EXPIRED entry whose
    heartbeat is still recent.
    """


class LedgerStorageUnavailableError(LedgerError):
    """Raised when the durable storage backend fails mid-operation.

    Fail-closed contract: storage down during a claim means the tool never
    runs; storage down after the effect (``complete`` / failure recording)
    propagates and leaves the entry ``IN_FLIGHT``, which later resolves via
    lease expiry → ``EXPIRED`` → hard-block/reconcile. The original backend
    exception is preserved as ``__cause__``.
    """


# Verified outcomes accepted by ActionLedger.release().
OPERATOR_RESOLUTION_COMPLETED = "completed"
OPERATOR_RESOLUTION_NOT_EXECUTED = "not_executed"

# Stored terminal-outcome values that resolution paths (release, reconcile)
# will accept from existing entries.  IN_FLIGHT (None) and COMPLETED are missing
# because resolution paths should never see them at write time.
_RESOLUTION_ACCEPTED_STORED_OUTCOMES: frozenset[str] = frozenset({
    TerminalOutcome.IN_FLIGHT.value,
    TerminalOutcome.BLOCKED.value,
    TerminalOutcome.UNKNOWN.value,
    TerminalOutcome.FAILED_AFTER_EFFECT.value,
    TerminalOutcome.FAILED_BEFORE_EFFECT.value,
})

# Stored terminal-outcome values that **the NOT_EXECUTED reset** accepts.
# Excludes ``IN_FLIGHT`` so two reconcilers racing ``NOT_EXECUTED``
# cannot both transition ``IN_FLIGHT → IN_FLIGHT`` — only the first
# writer wins; the second sees ``IN_FLIGHT`` and fails the CAS.
# EXPIRED entries (stored ``IN_FLIGHT`` with expired lease) are advanced
# to ``BLOCKED`` before the CAS (see ``_apply_reconcile_result``).
_RECONCILE_NOT_EXECUTED_OUTCOMES: frozenset[str] = frozenset({
    TerminalOutcome.BLOCKED.value,
    TerminalOutcome.UNKNOWN.value,
    TerminalOutcome.FAILED_AFTER_EFFECT.value,
    TerminalOutcome.FAILED_BEFORE_EFFECT.value,
})

# Policies for tools ledgered without a transition_binding (unclassified).
# "warn": legacy behavior + a one-time warning when a failed entry is
# reclaimed. "strict": route the claim through claim_side_effecting with a
# conservative synthesized binding so failed retries hard-block.
UNCLASSIFIED_POLICY_WARN = "warn"
UNCLASSIFIED_POLICY_STRICT = "strict"

# Conservative binding synthesized for "strict" unclassified claims:
# NON_IDEMPOTENT_MUTATE yields MANUAL_RECONCILIATION_REQUIRED + SINGLE_USE
# from the existing class defaults. Request-id derivation stays legacy.
_UNCLASSIFIED_BINDING = ToolTransitionBinding.for_tool(
    agent_id="unclassified",
    policy_version="unclassified",
    side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
)


def _ledger_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


# Boundary ordering: a transition may only move forward toward CROSSED.
_BOUNDARY_RANK: dict[SideEffectBoundary, int] = {
    SideEffectBoundary.NOT_CROSSED: 0,
    SideEffectBoundary.MAYBE_CROSSED: 1,
    SideEffectBoundary.CROSSED: 2,
}

# Expected terminal outcomes for a wrapper-path transition write (IN_FLIGHT).
_IN_FLIGHT_OUTCOMES: frozenset[str] = frozenset({TerminalOutcome.IN_FLIGHT.value})


# Resolved outcomes that park a transition until a human releases it.
_STUCK_OUTCOMES = frozenset(
    {
        TerminalOutcome.BLOCKED,
        TerminalOutcome.UNKNOWN,
        TerminalOutcome.FAILED_AFTER_EFFECT,
        TerminalOutcome.EXPIRED,
    }
)


def _format_heartbeat_age(entry: LedgerEntry, *, now: float) -> str:
    """Human-readable age of the last heartbeat (or started_at fallback)."""
    ref = entry.last_heartbeat_at if entry.last_heartbeat_at is not None else entry.started_at
    age = now - ref
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age // 60)}m ago"
    return f"{int(age // 3600)}h ago"


def _grace_remaining(
    entry: LedgerEntry,
    *,
    now: float,
    presumed_dead_after: float,
) -> str:
    """Human-readable time until the grace window elapses."""
    ref = entry.last_heartbeat_at if entry.last_heartbeat_at is not None else entry.started_at
    remaining = presumed_dead_after - (now - ref)
    if remaining <= 0:
        return "now"
    if remaining < 60:
        return f"{int(remaining)}s"
    if remaining < 3600:
        return f"{int(remaining // 60)}m"
    return f"{int(remaining // 3600)}h"


def _is_stuck_transition(
    entry: LedgerEntry,
    resolved: TerminalOutcome,
    *,
    now: float,
    in_flight_stuck_after: float,
) -> bool:
    """Whether a transition needs operator attention (see list_transitions)."""
    if resolved in _STUCK_OUTCOMES:
        return True
    if resolved == TerminalOutcome.IN_FLIGHT and in_flight_stuck_after > 0:
        return now - entry.started_at > in_flight_stuck_after
    return False


@contextmanager
def _storage_errors(operation: str) -> Iterator[None]:
    """Re-raise backend storage failures as :class:`LedgerStorageUnavailableError`.

    Only wraps exceptions raised by the storage layer itself — ``LedgerError``
    subclasses (policy refusals, hard blocks) pass through unchanged, and tool
    exceptions never reach this boundary (the claim path never runs tool code).
    The backend exception is preserved as ``__cause__``.
    """
    try:
        yield
    except LedgerError:
        raise
    except Exception as exc:
        raise LedgerStorageUnavailableError(
            f"ledger storage unavailable during {operation}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


@dataclass(frozen=True)
class _ActiveTransition:
    """The side-effecting transition currently executing on this task/thread."""

    ledger: ActionLedger
    request_id: str
    binding: ToolTransitionBinding | None


_active_transition_var: ContextVar[_ActiveTransition | None] = ContextVar(
    "mycelium_active_transition",
    default=None,
)

# Set when _apply_reconcile_result or _raise_hard_block re-reads the entry and
# finds it already claimed by another thread (CAS-loss or stale snapshot).
# The claim loop checks this flag: if set, an IN_FLIGHT return means "poll",
# not "this thread won the fresh claim".
_reconcile_cas_lost: threading.local = threading.local()

# Set when a claim consumed a NOT_EXECUTED verdict (reconciler NOT_EXECUTED or
# an operator release verified "not_executed") and won the fresh in-flight
# claim. The @ledger wrapper reads this right after the claim to tag the
# resulting tool-body run as an *authorized* re-execution (never a silent
# duplicate). A ContextVar keeps concurrent async tasks isolated.
_outcome_reexec_authorized: ContextVar[bool] = ContextVar(
    "mycelium_outcome_reexec_authorized",
    default=False,
)


def get_active_transition() -> _ActiveTransition | None:
    """Return the transition currently executing in this context, if any."""
    return _active_transition_var.get()


def _advance_active_boundary(boundary: SideEffectBoundary) -> None:
    active = _active_transition_var.get()
    if active is None:
        warnings.warn(
            "side-effect boundary marker used outside a ledgered tool; ignored",
            stacklevel=3,
        )
        return
    active.ledger.advance_boundary(active.request_id, boundary)


def mark_maybe_crossed() -> None:
    """Mark the active transition as ``maybe_crossed``.

    Call immediately before performing the external operation. If the tool
    raises or the process crashes after this point, the durable entry retains
    ``maybe_crossed`` so a redispatch hard-blocks instead of re-executing a
    possibly-already-applied side effect.
    """
    _advance_active_boundary(SideEffectBoundary.MAYBE_CROSSED)


def mark_crossed() -> None:
    """Mark the active transition as ``crossed`` (effect definitely happened)."""
    _advance_active_boundary(SideEffectBoundary.CROSSED)


def record_external_operation(ref: str) -> None:
    """Attach the provider's operation handle to the active transition.

    ``ref`` is the external system's identifier for the effect this call
    produced — a provider id (e.g. Stripe ``pi_...``) or the idempotency key
    sent to the provider. It is stored durably so an ambiguous transition
    (``UNKNOWN`` / ``FAILED_AFTER_EFFECT`` / ``maybe_crossed``) can later be
    reconciled against the provider instead of hard-blocking blindly.

    Record it as early as possible — ideally the idempotency key *before* the
    call, or the returned id immediately after — inside ``side_effect()``.
    """
    active = _active_transition_var.get()
    if active is None:
        warnings.warn(
            "record_external_operation() used outside a ledgered tool; ignored",
            stacklevel=2,
        )
        return
    active.ledger.attach_external_operation_ref(active.request_id, ref)


def renew_lease(*, lease_ttl: float | None = None) -> None:
    """Extend the active transition's execution lease.

    ``@ledger`` / ``@ledger_sync`` already auto-renew while the tool body runs.
    Call this for an extra mid-flight bump, or when driving
    :meth:`ActionLedger.claim_side_effecting` yourself without the decorator.
    Peers still ``POLL`` on a held lease; incomplete durable fields are healed
    via ``ActionLedger.repair_transition``. Lease is resolution metadata (not
    part of ``transition_key``).

    Outside a ledgered tool this is a no-op with a warning.
    """
    active = _active_transition_var.get()
    if active is None:
        warnings.warn(
            "renew_lease() used outside a ledgered tool; ignored",
            stacklevel=2,
        )
        return
    active.ledger.renew_lease(active.request_id, lease_ttl=lease_ttl)


def _resolve_lease_renew_interval(
    lease_ttl: float,
    lease_renew_interval: float | None,
) -> float | None:
    """Return seconds between auto-renew ticks, or ``None`` to disable.

    ``lease_renew_interval <= 0`` disables auto-renew. ``None`` means
    ``lease_ttl * DEFAULT_LEASE_RENEW_RATIO`` (floored at
    :data:`MIN_LEASE_RENEW_INTERVAL`). Unbounded leases (``lease_ttl <= 0``)
    never auto-renew.
    """
    if lease_ttl <= 0:
        return None
    if lease_renew_interval is not None:
        if lease_renew_interval <= 0:
            return None
        return lease_renew_interval
    return max(lease_ttl * DEFAULT_LEASE_RENEW_RATIO, MIN_LEASE_RENEW_INTERVAL)


@contextmanager
def _lease_auto_renew(ledger: ActionLedger, request_id: str) -> Iterator[None]:
    """Background owner heartbeat while a ledgered tool body executes.

    Keeps ``lease_until`` ahead of wall clock so redispatched peers stay on
    ``POLL`` instead of treating a still-running worker as ``EXPIRED``.
    """
    interval = _resolve_lease_renew_interval(
        ledger._lease_ttl,
        ledger._lease_renew_interval,
    )
    if interval is None:
        yield
        return

    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(interval):
            try:
                ledger.renew_lease(request_id, lease_ttl=ledger._lease_ttl)
            except LedgerError as exc:
                _logger.warning(
                    "lease auto-renew stopped for %s: %s",
                    request_id,
                    exc,
                )
                return
            except Exception:
                _logger.exception(
                    "lease auto-renew failed for %s; will retry",
                    request_id,
                )

    thread = threading.Thread(
        target=_loop,
        name=f"mycelium-lease-renew:{request_id[:16]}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=max(interval, MIN_LEASE_RENEW_INTERVAL) + 1.0)


@contextmanager
def side_effect() -> Iterator[None]:
    """Wrap the external operation of a side-effecting tool.

    On entry the active transition advances to ``maybe_crossed``; on clean exit
    to ``crossed``. If the body raises, the boundary stays ``maybe_crossed`` so
    the failure is classified as ambiguous (``UNKNOWN``) rather than
    ``FAILED_BEFORE_EFFECT``::

        @ledger_sync(transition_binding=binding)
        def send_payment(amount, recipient):
            with side_effect():
                return gateway.charge(amount, recipient)
    """
    mark_maybe_crossed()
    yield
    mark_crossed()


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
    external_operation_ref: str | None = None
    provider_idempotency_key: str | None = None
    provider_key_first_attempt_at: float | None = None
    # Operator release (manual reconciliation) audit fields. Set once by
    # ActionLedger.release(); "not_executed" is consumed by the next claim.
    # Worker-death signal fields. ``last_heartbeat_at`` is set on claim and
    # updated by ``renew_lease()``; the auto-renew loop maintains it with no
    # further changes.  ``worker_dead_asserted_*`` is stamped by
    # ``mark_worker_dead()`` / ``mark_worker_dead_for()`` — the channel for
    # orchestrator death events (k8s OOM-kill hooks, LangGraph redispatch
    # sweeps) and humans.
    last_heartbeat_at: float | None = None
    worker_dead_asserted_by: str | None = None
    worker_dead_asserted_at: float | None = None

    # Operator release (manual reconciliation) audit fields. Set once by
    # ActionLedger.release(); "not_executed" is consumed by the next claim.
    operator_resolution: str | None = None  # "completed" | "not_executed"
    resolved_by: str | None = None
    resolution_reason: str | None = None
    resolved_at: float | None = None
    released_from_outcome: str | None = None

    # Optional state-authority / decision pass-through (audit only — enforcement
    # lives in ``state_authority.StateAuthority``, not in claim resolution).
    decision_id: str | None = None
    state_ref: str | None = None

    def __post_init__(self) -> None:
        # Match from_dict / claim: durable key defaults to request_id.
        if self.idempotency_key is None:
            object.__setattr__(self, "idempotency_key", self.request_id)

    def resolved_terminal_outcome(self, *, now: float | None = None) -> TerminalOutcome:
        return resolve_terminal_outcome(
            self.terminal_outcome,
            lease_until=self.lease_until,
            now=now,
        )

    def lease_validity(self, *, now: float | None = None) -> LeaseValidity:
        """Return whether this entry's execution lease is still held."""
        return resolve_lease_validity(self.lease_until, now=now)

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
            "external_operation_ref": self.external_operation_ref,
            "provider_idempotency_key": self.provider_idempotency_key,
            "provider_key_first_attempt_at": self.provider_key_first_attempt_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "worker_dead_asserted_by": self.worker_dead_asserted_by,
            "worker_dead_asserted_at": self.worker_dead_asserted_at,
            "operator_resolution": self.operator_resolution,
            "resolved_by": self.resolved_by,
            "resolution_reason": self.resolution_reason,
            "resolved_at": self.resolved_at,
            "released_from_outcome": self.released_from_outcome,
            "decision_id": self.decision_id,
            "state_ref": self.state_ref,
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
            external_operation_ref=data.get("external_operation_ref"),
            provider_idempotency_key=data.get("provider_idempotency_key"),
            provider_key_first_attempt_at=data.get("provider_key_first_attempt_at"),
            last_heartbeat_at=data.get("last_heartbeat_at"),
            worker_dead_asserted_by=data.get("worker_dead_asserted_by"),
            worker_dead_asserted_at=data.get("worker_dead_asserted_at"),
            operator_resolution=data.get("operator_resolution"),
            resolved_by=data.get("resolved_by"),
            resolution_reason=data.get("resolution_reason"),
            resolved_at=data.get("resolved_at"),
            released_from_outcome=data.get("released_from_outcome"),
            decision_id=(
                str(data["decision_id"])
                if data.get("decision_id") is not None
                else None
            ),
            state_ref=(
                str(data["state_ref"]) if data.get("state_ref") is not None else None
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

    def try_transition(
        self,
        entry: LedgerEntry,
        *,
        expected_terminal_outcomes: frozenset[str],
        expected_owner: str | None = None,
    ) -> bool:
        """Atomically write *entry* only if the stored entry's terminal outcome
        is one of *expected_terminal_outcomes* (and *expected_owner* matches,
        when set).

        Returns ``True`` when the write succeeds, ``False`` when the pre-condition
        is not met (caller raises ``LedgerOutcomeAlreadySetError``).

        The default implementation performs a get+set (single-process only).
        Override with an atomic compare-and-swap for multi-process backends.
        """
        existing = self.get(entry.request_id)
        if existing is None:
            return False
        if existing.terminal_outcome not in expected_terminal_outcomes:
            return False
        if expected_owner is not None and existing.owner != expected_owner:
            return False
        self.set(entry)
        return True

    def list_all(self) -> list[LedgerEntry]:
        """Return all entries. Intended for debugging/auditing only."""
        raise NotImplementedError


class InMemoryLedgerStorage(LedgerStorage):
    """Default in-memory storage. Survives within the process only.

    Thread-safe via ``_lock`` (``threading.RLock``) so concurrent in-process
    claims and transitions do not lose writes.  Multi-process users must
    choose a durable backend.
    """

    def __init__(self) -> None:
        self._entries: dict[str, LedgerEntry] = {}
        self._lock = threading.RLock()

    def get(self, request_id: str) -> LedgerEntry | None:
        with self._lock:
            return self._entries.get(request_id)

    def set(self, entry: LedgerEntry) -> None:
        with self._lock:
            self._entries[entry.request_id] = entry

    def try_claim_inflight(
        self,
        entry: LedgerEntry,
        *,
        lease_ttl: float = DEFAULT_LEASE_TTL,
    ) -> tuple[str, LedgerEntry | None]:
        with self._lock:
            return default_try_claim_inflight(self, entry, lease_ttl=lease_ttl)

    def try_transition(
        self,
        entry: LedgerEntry,
        *,
        expected_terminal_outcomes: frozenset[str],
        expected_owner: str | None = None,
    ) -> bool:
        with self._lock:
            existing = self._entries.get(entry.request_id)
            if existing is None:
                return False
            if existing.terminal_outcome not in expected_terminal_outcomes:
                return False
            if expected_owner is not None and existing.owner != expected_owner:
                return False
            self.set(entry)
            return True

    def list_all(self) -> list[LedgerEntry]:
        with self._lock:
            return list(self._entries.values())


class FileLedgerStorage(LedgerStorage):
    """JSON-file-backed storage with ``fcntl`` + threading locking.

    The ``fcntl`` lock guards across processes; the ``threading.Lock`` guards
    across threads within the same process (``flock`` has process-level
    semantics on macOS/Linux, so multiple threads cannot rely on it alone).
    """

    def __init__(self, path: str | Path) -> None:
        self._file = LockedJsonDictFile(path)
        self._lock = threading.Lock()

    def get(self, request_id: str) -> LedgerEntry | None:
        def read(data: dict[str, dict[str, Any]]) -> LedgerEntry | None:
            raw = data.get(request_id)
            if raw is None:
                return None
            return LedgerEntry.from_dict(raw)

        with self._lock:
            return self._file.read_modify_write_no_save(read)

    def set(self, entry: LedgerEntry) -> None:
        def mutate(data: dict[str, dict[str, Any]]) -> None:
            data[entry.request_id] = entry.to_dict()

        with self._lock:
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

        with self._lock:
            self._file.read_modify_write(mutate)
        return outcome[0]

    def try_transition(
        self,
        entry: LedgerEntry,
        *,
        expected_terminal_outcomes: frozenset[str],
        expected_owner: str | None = None,
    ) -> bool:
        result: list[bool] = []

        def mutate(data: dict[str, dict[str, Any]]) -> None:
            raw = data.get(entry.request_id)
            if raw is None:
                result.append(False)
                return
            existing = LedgerEntry.from_dict(raw)
            if existing.terminal_outcome not in expected_terminal_outcomes:
                result.append(False)
                return
            if expected_owner is not None and existing.owner != expected_owner:
                result.append(False)
                return
            data[entry.request_id] = entry.to_dict()
            result.append(True)

        with self._lock:
            self._file.read_modify_write(mutate)
        return result[0]

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
        lease_renew_interval: float | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        poll_timeout: float | None = DEFAULT_POLL_TIMEOUT,
        reconciler: Reconciler | None = None,
        defer_read_only_unknown: bool = False,
        audit_emitter: AuditReceiptEmitter | None = None,
        outcome_emitter: OutcomeEmitter | None = None,
        unclassified_policy: str = UNCLASSIFIED_POLICY_WARN,
        reclaim_requires_death_signal: bool = False,
        presumed_dead_after: float | None = None,
    ) -> None:
        self._storage = storage if storage is not None else InMemoryLedgerStorage()
        self._lease_ttl = lease_ttl
        # None → renew at lease_ttl/3 while @ledger tool bodies run; <=0 disables.
        self._lease_renew_interval = lease_renew_interval
        self._poll_interval = poll_interval
        self._poll_timeout = poll_timeout
        self._reconciler = reconciler
        # Read-only UNKNOWN/BLOCKED gate resolution: when False (default) the
        # ambiguous state is safely re-run (SOFT_BLOCK -> retry); when True the
        # claim raises LedgerSoftBlockError so the caller can defer the retry.
        self._defer_read_only_unknown = defer_read_only_unknown
        # Optional receipt sink for operator releases (release() emits here).
        self._audit_emitter = audit_emitter
        # Optional resolution-telemetry sink (see mycelium.outcome_emit).
        self._outcome_emitter = outcome_emitter
        if unclassified_policy not in (
            UNCLASSIFIED_POLICY_WARN,
            UNCLASSIFIED_POLICY_STRICT,
        ):
            raise ValueError(
                f"unclassified_policy must be {UNCLASSIFIED_POLICY_WARN!r} or "
                f"{UNCLASSIFIED_POLICY_STRICT!r}, got {unclassified_policy!r}"
            )
        # Policy for claims without a transition_binding (unclassified tools).
        self._unclassified_policy = unclassified_policy
        self._memory_warned_tools: set[str] = set()
        self._unclassified_warned_tools: set[str] = set()
        # Worker-death signal: when True, EXPIRED entries cannot be reclaimed
        # without affirmative death evidence (mark_worker_dead or heartbeat
        # older than presumed_dead_after). Default False preserves existing
        # behavior exactly.
        self._reclaim_requires_death_signal = reclaim_requires_death_signal
        # Grace window: seconds since last heartbeat (or started_at) after
        # which a worker is presumed dead. Default 2x lease_ttl.
        self._presumed_dead_after = (
            presumed_dead_after
            if presumed_dead_after is not None
            else lease_ttl * DEFAULT_PRESUMED_DEAD_AFTER_RATIO
        )

    # --- storage boundary (fail-closed; see LedgerStorageUnavailableError) ---

    def _get_entry(self, request_id: str) -> LedgerEntry | None:
        with _storage_errors("get"):
            return self._storage.get(request_id)

    def _set_entry(self, entry: LedgerEntry) -> None:
        with _storage_errors("set"):
            self._storage.set(entry)

    def _try_claim_inflight(
        self,
        entry: LedgerEntry,
        *,
        lease_ttl: float,
    ) -> tuple[str, LedgerEntry | None]:
        with _storage_errors("try_claim_inflight"):
            return self._storage.try_claim_inflight(entry, lease_ttl=lease_ttl)

    def _try_transition(
        self,
        entry: LedgerEntry,
        *,
        expected_from: frozenset[str] | None = None,
        expected_owner: str | None = None,
    ) -> bool:
        """Atomically write *entry* subject to outcome/owner pre-conditions.

        Returns ``True`` on success, ``False`` when the stored entry's
        terminal outcome is not in *expected_from* (or owner mismatch).
        The caller raises ``LedgerOutcomeAlreadySetError`` on ``False``.
        """
        outcomes = expected_from if expected_from is not None else _IN_FLIGHT_OUTCOMES
        with _storage_errors("try_transition"):
            return self._storage.try_transition(
                entry,
                expected_terminal_outcomes=outcomes,
                expected_owner=expected_owner,
            )

    def _list_all_entries(self) -> list[LedgerEntry]:
        with _storage_errors("list_all"):
            return self._storage.list_all()

    # --- resolution telemetry (opt-in; never raises, never disturbs the path) ---

    def _emit_outcome(
        self,
        *,
        request_id: str,
        tool: str,
        event: str,
        gate: str | None = None,
        terminal_outcome: TerminalOutcome | None = None,
        boundary: SideEffectBoundary | None = None,
        side_effect_class: SideEffectClass | None = None,
        tool_body_executed: bool = False,
        dispatch_attempt: int | None = None,
        authorized_reexec: bool = False,
        owner: str | None = None,
        error_class: str | None = None,
    ) -> None:
        """Emit one outcome row, backfilling state from the stored entry.

        Fault-tolerant by design: any failure (including a storage read) is
        logged and swallowed so telemetry can never alter claim/CAS/reconcile
        semantics or break the tool path.
        """
        if self._outcome_emitter is None:
            return
        try:
            entry = self.get(request_id)
        except Exception:
            entry = None
        if entry is not None:
            if terminal_outcome is None:
                terminal_outcome = entry.resolved_terminal_outcome()
            if boundary is None:
                boundary = SideEffectBoundary(entry.side_effect_boundary)
        try:
            self._outcome_emitter.emit_event(
                tool=tool,
                request_id=request_id,
                event=event,
                gate=gate,
                terminal_outcome=(
                    terminal_outcome.value if terminal_outcome is not None else None
                ),
                side_effect_boundary=boundary.value if boundary is not None else None,
                side_effect_class=(
                    side_effect_class.value if side_effect_class is not None else None
                ),
                tool_body_executed=tool_body_executed,
                dispatch_attempt=dispatch_attempt,
                authorized_reexec=authorized_reexec,
                owner=owner,
                error_class=error_class,
            )
        except Exception:
            _logger.exception("failed to emit outcome row for %s", request_id)

    # --- one-time operator warnings ---

    def _warn_if_volatile_side_effect_storage(
        self,
        tool: str,
        binding: ToolTransitionBinding,
    ) -> None:
        """Warn once per (ledger, tool) when a side-effecting claim uses memory.

        Memory is the legitimate dev/demo backend, so this is a warning, not
        an error — but the no-duplicate-side-effects guarantee only holds
        within the process while claims live in ``InMemoryLedgerStorage``.
        """
        if binding.side_effect_class == SideEffectClass.READ:
            return
        if not isinstance(self._storage, InMemoryLedgerStorage):
            return
        if tool in self._memory_warned_tools:
            return
        self._memory_warned_tools.add(tool)
        warnings.warn(
            f"Tool {tool!r} is side-effecting ({binding.side_effect_class.value}) "
            "but its ActionLedger uses InMemoryLedgerStorage: claims are not "
            "durable across processes or restarts, so the duplicate-side-effect "
            "guard only holds within this process. Use file/sqlite/redis/postgres "
            "storage beyond local dev/demo.",
            stacklevel=3,
        )

    def _warn_unclassified_retry(self, tool: str, existing: LedgerEntry | None) -> None:
        """Warn once per tool before a binding-less claim reclaims a failed entry.

        Without a ``transition_binding`` Mycelium cannot know whether the tool
        has side effects, so the legacy claim path reclaims failed entries —
        which may duplicate an external effect. Set
        ``unclassified_policy="strict"`` to hard-block these retries instead.
        """
        if existing is None or tool in self._unclassified_warned_tools:
            return
        if existing.resolved_terminal_outcome() not in (
            TerminalOutcome.FAILED_BEFORE_EFFECT,
            TerminalOutcome.FAILED_AFTER_EFFECT,
        ):
            return
        self._unclassified_warned_tools.add(tool)
        warnings.warn(
            f"Tool {tool!r} was ledgered without a transition_binding, so "
            "Mycelium cannot know whether it has side effects — retrying its "
            "previously-failed claim may duplicate an external effect. Declare "
            "side_effect_class / a transition_binding, or set "
            "unclassified_policy='strict' to hard-block failed retries.",
            stacklevel=4,
        )

    # --- public API ---

    def get(self, request_id: str) -> LedgerEntry | None:
        return self._get_entry(request_id)

    def list_transitions(
        self,
        *,
        stuck: bool = False,
        tool: str | None = None,
        outcome: TerminalOutcome | None = None,
        in_flight_stuck_after: float = DEFAULT_LEASE_TTL,
    ) -> list[LedgerEntry]:
        """List ledger entries for operator triage (read-only).

        ``stuck=True`` keeps transitions that need a human: resolved terminal
        outcome ``BLOCKED`` / ``UNKNOWN`` / ``FAILED_AFTER_EFFECT`` /
        ``EXPIRED``, plus ``IN_FLIGHT`` entries older than
        ``in_flight_stuck_after`` seconds (an in-flight entry whose lease can
        never expire — e.g. unbounded — would otherwise be invisible forever).
        ``tool`` filters by tool name; ``outcome`` filters by the resolved
        terminal outcome (lease validity applied). Sorted oldest first.
        """
        now = time.time()
        entries: list[LedgerEntry] = []
        for entry in self._list_all_entries():
            if tool is not None and entry.tool != tool:
                continue
            resolved = entry.resolved_terminal_outcome(now=now)
            if outcome is not None and resolved != outcome:
                continue
            if stuck and not _is_stuck_transition(
                entry,
                resolved,
                now=now,
                in_flight_stuck_after=in_flight_stuck_after,
            ):
                continue
            entries.append(entry)
        entries.sort(key=lambda entry: entry.started_at)
        return entries

    def release(
        self,
        request_id: str,
        *,
        verified: str,
        result: Any = None,
        by: str,
        reason: str,
    ) -> LedgerEntry:
        """Record a human verification that releases a hard-blocked transition.

        This is a *recorded verification*, not an unblock: the operator must
        first check the external provider (via ``external_operation_ref`` /
        ``provider_idempotency_key`` on the entry) and attest to one of two
        verified outcomes:

        - ``verified="completed"`` — the effect happened. The transition is
          marked completed with ``result``; the next redispatch returns it
          without re-executing.
        - ``verified="not_executed"`` — the effect provably never happened.
          Only the resolution is stamped here; the next claim consumes it and
          grants exactly one re-execution (one-shot).

        Fail-closed (typed exceptions): unknown request, already-resolved
        entry (one-shot, never overwritten), already-``COMPLETED`` transition,
        and ``IN_FLIGHT`` with a still-held lease are all refused. Entries are
        never deleted — the release is stamped on the durable record so
        ``provider_idempotency_key`` enforcement and audit history survive.
        """
        if verified not in (
            OPERATOR_RESOLUTION_COMPLETED,
            OPERATOR_RESOLUTION_NOT_EXECUTED,
        ):
            raise LedgerReleaseRefusedError(
                f"verified must be {OPERATOR_RESOLUTION_COMPLETED!r} or "
                f"{OPERATOR_RESOLUTION_NOT_EXECUTED!r}, got {verified!r}"
            )
        if not by:
            raise LedgerReleaseRefusedError("release requires an operator identity ('by')")
        if not reason:
            raise LedgerReleaseRefusedError("release requires a reason")
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerReleaseRefusedError(
                f"Cannot release unknown request {request_id!r}"
            )
        if existing.operator_resolution is not None:
            raise LedgerAlreadyResolvedError(
                f"Request {request_id!r} already has an operator resolution "
                f"({existing.operator_resolution!r} by {existing.resolved_by!r}); "
                "release is one-shot"
            )
        now = time.time()
        outcome = existing.resolved_terminal_outcome(now=now)
        if outcome == TerminalOutcome.COMPLETED:
            raise LedgerReleaseRefusedError(
                f"Cannot release request {request_id!r}: already COMPLETED"
            )
        if outcome == TerminalOutcome.IN_FLIGHT:
            # Resolved IN_FLIGHT means the lease is HELD or UNBOUNDED (an
            # expired lease resolves to EXPIRED). A worker may still be alive.
            raise LedgerReleaseRefusedError(
                f"Cannot release request {request_id!r}: IN_FLIGHT with a "
                f"{existing.lease_validity(now=now).value} lease — wait for "
                "the lease to expire (EXPIRED is releasable)"
            )
        if outcome == TerminalOutcome.EXPIRED:
            # EXPIRED with a recent heartbeat means the worker may still be
            # alive (GC pause, storage partition, silently failing auto-renew).
            # When reclaim_requires_death_signal is on, refuse until the grace
            # window elapses or death is asserted.
            if (
                self._reclaim_requires_death_signal
                and not has_worker_death_evidence(
                    existing, now=now,
                    presumed_dead_after=self._presumed_dead_after,
                )
            ):
                grace = _grace_remaining(
                    existing, now=now,
                    presumed_dead_after=self._presumed_dead_after,
                )
                raise LedgerWorkerAliveError(
                    f"Cannot release request {request_id!r}: EXPIRED but "
                    f"worker appears alive "
                    f"({_format_heartbeat_age(existing, now=now)}) — "
                    f"grace window elapses in {grace}. "
                    "Use mark_worker_dead() first, or wait for the grace window."
                )
        if verified == OPERATOR_RESOLUTION_COMPLETED:
            completed = self.complete(
                request_id,
                result,
                _expected_from=_RESOLUTION_ACCEPTED_STORED_OUTCOMES,
            )
            entry = replace(
                completed,
                operator_resolution=OPERATOR_RESOLUTION_COMPLETED,
                resolved_by=by,
                resolution_reason=reason,
                resolved_at=now,
                released_from_outcome=outcome.value,
            )
        else:
            entry = replace(
                existing,
                operator_resolution=OPERATOR_RESOLUTION_NOT_EXECUTED,
                resolved_by=by,
                resolution_reason=reason,
                resolved_at=now,
                released_from_outcome=outcome.value,
            )
        self._set_entry(entry)
        self._emit_outcome(
            request_id=request_id,
            tool=entry.tool,
            event="release",
            gate="RELEASE",
            terminal_outcome=entry.resolved_terminal_outcome(now=now),
            boundary=SideEffectBoundary(entry.side_effect_boundary),
            authorized_reexec=(verified == OPERATOR_RESOLUTION_NOT_EXECUTED),
            owner=by,
        )
        if self._audit_emitter is not None:
            receipt = self._audit_emitter.emit_release_receipt(
                entry,
                verified=verified,
                by=by,
                reason=reason,
            )
            entry = self.attach_receipt_ref(request_id, receipt.receipt_id)
        return entry

    def _new_inflight_entry(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        binding: ToolTransitionBinding | None = None,
        _provider_key_first_attempt_at: float | None = None,
    ) -> LedgerEntry:
        bound = _bind_args(args, kwargs)
        boundary = (
            binding.side_effect_boundary_default.value
            if binding is not None
            else SideEffectBoundary.NOT_CROSSED.value
        )
        provider_key = (
            extract_provider_idempotency_key(kwargs, binding)
            if binding is not None
            else None
        )
        if provider_key is not None and _provider_key_first_attempt_at is None:
            pkey_first_attempt: float | None = time.time()
        else:
            pkey_first_attempt = _provider_key_first_attempt_at
        decision_raw = kwargs.get("decision_id")
        state_ref_raw = kwargs.get("state_ref")
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
            provider_idempotency_key=provider_key,
            provider_key_first_attempt_at=pkey_first_attempt,
            decision_id=str(decision_raw) if decision_raw is not None else None,
            state_ref=str(state_ref_raw) if state_ref_raw is not None else None,
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

        This is the legacy *unclassified* path (no ``transition_binding``), so
        Mycelium cannot know whether the tool has side effects. With
        ``unclassified_policy="warn"`` (default) a reclaim of a
        previously-failed entry proceeds but emits a one-time warning per
        tool. With ``unclassified_policy="strict"`` the claim is routed
        through :meth:`claim_side_effecting` with a conservative synthesized
        binding (``non_idempotent_mutate``): failed retries hard-block and an
        in-flight request polls instead of raising ``LedgerPendingError``.
        Request-id derivation stays legacy either way — only the resolution
        gate changes.
        """
        if self._unclassified_policy == UNCLASSIFIED_POLICY_STRICT:
            return self.claim_side_effecting(
                request_id,
                tool,
                args,
                kwargs,
                _UNCLASSIFIED_BINDING,
                lease_ttl=lease_ttl,
            )
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        self._warn_unclassified_retry(tool, self._get_entry(request_id))
        entry = self._new_inflight_entry(request_id, tool, args, kwargs)
        outcome, existing = self._try_claim_inflight(entry, lease_ttl=ttl)
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
            existing = self.get(request_id)
            if existing is not None:
                gate = resolve_read_only_gate(existing)
                if gate == TransitionGate.REPAIR:
                    self.repair_transition(request_id)
                    continue
                if gate == TransitionGate.RECLAIM and self._reclaim_requires_death_signal:
                    if not has_worker_death_evidence(
                        existing,
                        now=time.time(),
                        presumed_dead_after=self._presumed_dead_after,
                    ):
                        self._poll_read_only(
                            request_id,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue
                if gate == TransitionGate.SOFT_BLOCK:
                    return self._resolve_read_only_soft_block(
                        request_id, tool, args, kwargs, existing
                    )

            entry = self._new_inflight_entry(request_id, tool, args, kwargs)
            outcome, existing = self._try_claim_inflight(entry, lease_ttl=ttl)
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

    def _resolve_read_only_soft_block(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        existing: LedgerEntry,
    ) -> LedgerEntry:
        """Resolve a read-only ``SOFT_BLOCK`` (``UNKNOWN`` / ``BLOCKED``).

        Re-running a read-only tool is always safe, so by default the ambiguous
        entry is reset to a fresh in-flight claim and the tool runs exactly once
        more. When the ledger is configured with ``defer_read_only_unknown``,
        raise :class:`LedgerSoftBlockError` instead so an expensive read can be
        deferred and retried by the caller (cost-dependent).
        """
        if self._defer_read_only_unknown:
            raise LedgerSoftBlockError(
                soft_block_message(existing, tool=tool, request_id=request_id)
            )
        fresh = self._new_inflight_entry(request_id, tool, args, kwargs)
        self._set_entry(fresh)
        return fresh

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
            existing = self.get(request_id)
            if existing is not None:
                gate = resolve_read_only_gate(existing)
                if gate == TransitionGate.REPAIR:
                    self.repair_transition(request_id)
                    continue
                if gate == TransitionGate.RECLAIM and self._reclaim_requires_death_signal:
                    if not has_worker_death_evidence(
                        existing,
                        now=time.time(),
                        presumed_dead_after=self._presumed_dead_after,
                    ):
                        await self._poll_read_only_async(
                            request_id,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue
                if gate == TransitionGate.SOFT_BLOCK:
                    return self._resolve_read_only_soft_block(
                        request_id, tool, args, kwargs, existing
                    )

            entry = self._new_inflight_entry(request_id, tool, args, kwargs)
            outcome, existing = self._try_claim_inflight(entry, lease_ttl=ttl)
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
        *,
        binding: ToolTransitionBinding | None = None,
        now: float | None = None,
    ) -> LedgerEntry:
        current = self.get(request_id)
        if current is not None:
            curr_outcome = current.resolved_terminal_outcome(now=now)
            if curr_outcome == TerminalOutcome.IN_FLIGHT:
                _reconcile_cas_lost.val = True
                return current
            if curr_outcome == TerminalOutcome.COMPLETED:
                return current
            if curr_outcome == TerminalOutcome.EXPIRED:
                boundary = SideEffectBoundary(current.side_effect_boundary)
                if boundary == SideEffectBoundary.NOT_CROSSED:
                    error = (
                        "stale in-flight lease with not_crossed boundary; "
                        "reclaim only if an external_operation_ref reconcile "
                        "proves NOT_EXECUTED"
                    )
                else:
                    error = (
                        "stale in-flight lease; side-effect boundary "
                        f"{boundary.value} — effect may have happened"
                    )
                try:
                    existing = self.mark_blocked(
                        request_id,
                        error=error,
                        _expected_from=_IN_FLIGHT_OUTCOMES,
                        _expected_owner=current.owner,
                    )
                except LedgerOutcomeAlreadySetError:
                    again = self.get(request_id)
                    if again is not None:
                        _reconcile_cas_lost.val = True
                        return again
                    existing = current
        message = hard_block_message(
            existing, tool=tool, request_id=request_id, binding=binding, now=now
        )
        raise LedgerHardBlockError(message)

    def _apply_reconcile_result(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        binding: ToolTransitionBinding,
        result: Any,
        _preserved_pkey_first_attempt: float | None = None,
        _cas_race_returns_none: bool = False,
    ) -> LedgerEntry | None:
        """Map a reconcile result onto the ledger.

        ``COMPLETED`` marks the transition done (redispatch returns the stored
        result, no re-execution). ``NOT_EXECUTED`` resets the entry to a fresh
        in-flight claim so the tool runs exactly once. ``UNKNOWN`` returns None
        so the caller hard-blocks.

        When ``_cas_race_returns_none`` is True (operator-resolution path),
        a lost CAS returns None so the caller can fall through. Otherwise
        (reconciler path) the winner's entry is returned so the claim loop
        polls instead of hard-blocking.
        """
        if result.status == ReconcileStatus.COMPLETED:
            return self.complete(
                request_id,
                result.result,
                _expected_from=_RESOLUTION_ACCEPTED_STORED_OUTCOMES,
            )
        if result.status == ReconcileStatus.NOT_EXECUTED:
            if _preserved_pkey_first_attempt is None:
                old = self.get(request_id)
                if old is not None and old.provider_idempotency_key is not None:
                    _preserved_pkey_first_attempt = (
                        old.provider_key_first_attempt_at
                    )
            fresh = self._new_inflight_entry(
                request_id,
                tool,
                args,
                kwargs,
                binding=binding,
                _provider_key_first_attempt_at=_preserved_pkey_first_attempt,
            )
            # EXPIRED entries have stored terminal ``IN_FLIGHT`` (lease is
            # resolved at read time).  Advance past ``IN_FLIGHT`` first so the
            # CAS below cannot race on ``IN_FLIGHT → IN_FLIGHT``.
            now = time.time()
            stale = self.get(request_id)
            _stale_owner: str | None = stale.owner if stale is not None else None
            if stale is not None and stale.resolved_terminal_outcome(now=now) in (
                TerminalOutcome.EXPIRED,
            ):
                try:
                    self.mark_blocked(
                        request_id,
                        error="reconciling expired entry as NOT_EXECUTED",
                        _expected_from=_IN_FLIGHT_OUTCOMES,
                        _expected_owner=_stale_owner,
                    )
                except LedgerOutcomeAlreadySetError:
                    pass
                after_block = self.get(request_id)
                if (
                    after_block is not None
                    and after_block.terminal_outcome != TerminalOutcome.BLOCKED.value
                ):
                    if _cas_race_returns_none:
                        return None
                    _reconcile_cas_lost.val = True
                    return after_block
            if not self._try_transition(
                fresh,
                expected_from=_RECONCILE_NOT_EXECUTED_OUTCOMES,
            ):
                if _cas_race_returns_none:
                    return None
                _reconcile_cas_lost.val = True
                return self.get(request_id)
            # The fresh claim was won by this caller, which will run the tool
            # body exactly once — mark that run as an authorized re-execution
            # so outcome telemetry can tell it apart from a silent duplicate.
            _outcome_reexec_authorized.set(True)
            return fresh
        return None

    def _attempt_reconcile(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        existing: LedgerEntry,
        binding: ToolTransitionBinding,
    ) -> LedgerEntry | None:
        """Reconcile an ambiguous transition; None means fall through to block.

        Fail-closed: a missing reconciler, missing ref, or a raising reconciler
        all resolve to None (hard-block).
        """
        if self._reconciler is None or not existing.external_operation_ref:
            return None
        try:
            result = self._reconciler.reconcile(existing)
        except Exception:
            return None
        return self._apply_reconcile_result(
            request_id, tool, args, kwargs, binding, result
        )

    async def _attempt_reconcile_async(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        existing: LedgerEntry,
        binding: ToolTransitionBinding,
    ) -> LedgerEntry | None:
        """Async variant of :meth:`_attempt_reconcile`.

        Prefers ``reconcile_async`` when the reconciler provides it, otherwise
        falls back to the sync :meth:`Reconciler.reconcile`.
        """
        if self._reconciler is None or not existing.external_operation_ref:
            return None
        try:
            reconcile_async = getattr(self._reconciler, "reconcile_async", None)
            if reconcile_async is not None:
                result = await reconcile_async(existing)
            else:
                result = self._reconciler.reconcile(existing)
        except Exception:
            return None
        return self._apply_reconcile_result(
            request_id, tool, args, kwargs, binding, result
        )

    def _consume_operator_resolution(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        existing: LedgerEntry,
        binding: ToolTransitionBinding,
    ) -> LedgerEntry | None:
        """Consume an unconsumed operator ``not_executed`` release, if present.

        An operator release is the human-issued, durably stored equivalent of
        ``ReconcileResult.not_executed()``, so it reuses the same machinery:
        the entry resets to a fresh in-flight claim and the tool may execute
        exactly once. The fresh entry has ``operator_resolution=None`` (the
        release is one-shot) but carries the audit fields forward. Race
        characteristics match the Reconciler NOT_EXECUTED path (plain
        ``storage.set``).
        """
        if existing.operator_resolution != OPERATOR_RESOLUTION_NOT_EXECUTED:
            return None
        _preserved = (
            existing.provider_key_first_attempt_at
            if existing.provider_idempotency_key is not None
            else None
        )
        fresh = self._apply_reconcile_result(
            request_id,
            tool,
            args,
            kwargs,
            binding,
            ReconcileResult.not_executed(),
            _preserved_pkey_first_attempt=_preserved,
            _cas_race_returns_none=True,
        )
        if fresh is None:
            return None
        stamped = replace(
            fresh,
            resolved_by=existing.resolved_by,
            resolution_reason=existing.resolution_reason,
            resolved_at=existing.resolved_at,
            released_from_outcome=existing.released_from_outcome,
        )
        self._set_entry(stamped)
        return stamped

    def _reconcile_or_hard_block(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        existing: LedgerEntry,
        binding: ToolTransitionBinding,
    ) -> LedgerEntry:
        released = self._consume_operator_resolution(
            request_id, tool, args, kwargs, existing, binding
        )
        if released is not None:
            return released
        resolved = self._attempt_reconcile(
            request_id, tool, args, kwargs, existing, binding
        )
        if resolved is not None:
            return resolved
        return self._raise_hard_block(request_id, tool, existing, binding=binding)

    async def _reconcile_or_hard_block_async(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        existing: LedgerEntry,
        binding: ToolTransitionBinding,
    ) -> LedgerEntry:
        released = self._consume_operator_resolution(
            request_id, tool, args, kwargs, existing, binding
        )
        if released is not None:
            return released
        resolved = await self._attempt_reconcile_async(
            request_id, tool, args, kwargs, existing, binding
        )
        if resolved is not None:
            return resolved
        return self._raise_hard_block(request_id, tool, existing, binding=binding)

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
        self._warn_if_volatile_side_effect_storage(tool, binding)
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        interval = self._poll_interval if poll_interval is None else poll_interval
        timeout = self._poll_timeout if poll_timeout is None else poll_timeout
        poll_deadline = time.time() + timeout if timeout is not None else None
        incoming_key = extract_provider_idempotency_key(kwargs, binding)

        while True:
            existing = self.get(request_id)
            if existing is not None:
                gate = resolve_side_effect_gate(
                    existing,
                    binding,
                    incoming_provider_idempotency_key=incoming_key,
                )
                if gate == TransitionGate.REPAIR:
                    self.repair_transition(request_id)
                    continue
                if gate == TransitionGate.RETURN:
                    return existing
                if gate == TransitionGate.HARD_BLOCK:
                    entry = self._reconcile_or_hard_block(
                        request_id, tool, args, kwargs, existing, binding
                    )
                    if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                        if getattr(_reconcile_cas_lost, 'val', False):
                            _reconcile_cas_lost.val = False
                            self._poll_side_effecting(
                                request_id,
                                tool=tool,
                                interval=interval,
                                poll_deadline=poll_deadline,
                            )
                            continue
                    return entry
                if gate == TransitionGate.POLL:
                    self._poll_side_effecting(
                        request_id,
                        tool=tool,
                        interval=interval,
                        poll_deadline=poll_deadline,
                    )
                    continue
                if gate == TransitionGate.ALLOW and self._reclaim_requires_death_signal:
                    if not has_worker_death_evidence(
                        existing,
                        now=time.time(),
                        presumed_dead_after=self._presumed_dead_after,
                    ):
                        self._poll_side_effecting(
                            request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue

            _old_pkey_attempt = (
                existing.provider_key_first_attempt_at
                if existing is not None
                and existing.provider_idempotency_key is not None
                else None
            )
            entry = self._new_inflight_entry(
                request_id,
                tool,
                args,
                kwargs,
                binding=binding,
                _provider_key_first_attempt_at=_old_pkey_attempt,
            )
            outcome, existing = self._try_claim_inflight(entry, lease_ttl=ttl)
            if outcome == "completed" and existing is not None:
                return existing
            if outcome == "in_flight" and existing is not None:
                gate = resolve_side_effect_gate(
                    existing,
                    binding,
                    incoming_provider_idempotency_key=incoming_key,
                )
                if gate == TransitionGate.REPAIR:
                    self.repair_transition(request_id)
                    continue
                if gate == TransitionGate.RETURN:
                    return existing
                if gate == TransitionGate.HARD_BLOCK:
                    entry = self._reconcile_or_hard_block(
                        request_id, tool, args, kwargs, existing, binding
                    )
                    if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                        if getattr(_reconcile_cas_lost, 'val', False):
                            _reconcile_cas_lost.val = False
                            self._poll_side_effecting(
                                request_id,
                                tool=tool,
                                interval=interval,
                                poll_deadline=poll_deadline,
                            )
                            continue
                    return entry
                if gate == TransitionGate.ALLOW and self._reclaim_requires_death_signal:
                    if not has_worker_death_evidence(
                        existing,
                        now=time.time(),
                        presumed_dead_after=self._presumed_dead_after,
                    ):
                        self._poll_side_effecting(
                            request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue
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
                entry = self._reconcile_or_hard_block(
                    request_id, tool, args, kwargs, existing, binding
                )
                if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                    if getattr(_reconcile_cas_lost, 'val', False):
                        _reconcile_cas_lost.val = False
                        self._poll_side_effecting(
                            request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue
                    return entry
                return entry
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
        """Wait for an in-flight side-effecting transition; never auto-reclaim.

        When the lease expires mid-poll, return so the outer claim loop can
        re-resolve the gate and attempt provider reconcile before hard-blocking.
        """
        while True:
            if poll_deadline is not None and time.time() >= poll_deadline:
                current = self.get(request_id)
                if current is not None:
                    self.mark_unknown(
                        request_id,
                        error="timed out polling in-flight side-effecting transition",
                        _expected_from=_IN_FLIGHT_OUTCOMES,
                    )
                    raise LedgerHardBlockError(
                        hard_block_message(
                            current,
                            tool=tool,
                            request_id=request_id,
                        )
                    )
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
            # Leave EXPIRED to the outer claim loop so HARD_BLOCK can attempt
            # reconcile (EXPIRED + not_crossed + external_operation_ref →
            # reclaim only when the provider proves NOT_EXECUTED).
            if outcome == TerminalOutcome.EXPIRED:
                return
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
        self._warn_if_volatile_side_effect_storage(tool, binding)
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        interval = self._poll_interval if poll_interval is None else poll_interval
        timeout = self._poll_timeout if poll_timeout is None else poll_timeout
        poll_deadline = time.time() + timeout if timeout is not None else None
        incoming_key = extract_provider_idempotency_key(kwargs, binding)

        while True:
            existing = self.get(request_id)
            if existing is not None:
                gate = resolve_side_effect_gate(
                    existing,
                    binding,
                    incoming_provider_idempotency_key=incoming_key,
                )
                if gate == TransitionGate.REPAIR:
                    self.repair_transition(request_id)
                    continue
                if gate == TransitionGate.RETURN:
                    return existing
                if gate == TransitionGate.HARD_BLOCK:
                    entry = await self._reconcile_or_hard_block_async(
                        request_id, tool, args, kwargs, existing, binding
                    )
                    if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                        if getattr(_reconcile_cas_lost, 'val', False):
                            _reconcile_cas_lost.val = False
                            await self._poll_side_effecting_async(
                                request_id,
                                tool=tool,
                                interval=interval,
                                poll_deadline=poll_deadline,
                            )
                            continue
                    return entry
                if gate == TransitionGate.POLL:
                    await self._poll_side_effecting_async(
                        request_id,
                        tool=tool,
                        interval=interval,
                        poll_deadline=poll_deadline,
                    )
                    continue
                if gate == TransitionGate.ALLOW and self._reclaim_requires_death_signal:
                    if not has_worker_death_evidence(
                        existing,
                        now=time.time(),
                        presumed_dead_after=self._presumed_dead_after,
                    ):
                        await self._poll_side_effecting_async(
                            request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue

            _old_pkey_attempt = (
                existing.provider_key_first_attempt_at
                if existing is not None
                and existing.provider_idempotency_key is not None
                else None
            )
            entry = self._new_inflight_entry(
                request_id,
                tool,
                args,
                kwargs,
                binding=binding,
                _provider_key_first_attempt_at=_old_pkey_attempt,
            )
            outcome, existing = self._try_claim_inflight(entry, lease_ttl=ttl)
            if outcome == "completed" and existing is not None:
                return existing
            if outcome == "in_flight" and existing is not None:
                gate = resolve_side_effect_gate(
                    existing,
                    binding,
                    incoming_provider_idempotency_key=incoming_key,
                )
                if gate == TransitionGate.REPAIR:
                    self.repair_transition(request_id)
                    continue
                if gate == TransitionGate.RETURN:
                    return existing
                if gate == TransitionGate.HARD_BLOCK:
                    entry = await self._reconcile_or_hard_block_async(
                        request_id, tool, args, kwargs, existing, binding
                    )
                    if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                        if getattr(_reconcile_cas_lost, 'val', False):
                            _reconcile_cas_lost.val = False
                            await self._poll_side_effecting_async(
                                request_id,
                                tool=tool,
                                interval=interval,
                                poll_deadline=poll_deadline,
                            )
                            continue
                    return entry
                if gate == TransitionGate.ALLOW and self._reclaim_requires_death_signal:
                    if not has_worker_death_evidence(
                        existing,
                        now=time.time(),
                        presumed_dead_after=self._presumed_dead_after,
                    ):
                        await self._poll_side_effecting_async(
                            request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue
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
                entry = await self._reconcile_or_hard_block_async(
                    request_id, tool, args, kwargs, existing, binding
                )
                if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                    if getattr(_reconcile_cas_lost, 'val', False):
                        _reconcile_cas_lost.val = False
                        await self._poll_side_effecting_async(
                            request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue
                    return entry
                return entry
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
                        _expected_from=_IN_FLIGHT_OUTCOMES,
                    )
                    raise LedgerHardBlockError(
                        hard_block_message(
                            current,
                            tool=tool,
                            request_id=request_id,
                        )
                    )
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
            # Leave EXPIRED to the outer claim loop so HARD_BLOCK can attempt
            # reconcile (EXPIRED + not_crossed + external_operation_ref →
            # reclaim only when the provider proves NOT_EXECUTED).
            if outcome == TerminalOutcome.EXPIRED:
                return
            if outcome == TerminalOutcome.IN_FLIGHT:
                continue
            if outcome in (
                TerminalOutcome.FAILED_BEFORE_EFFECT,
                TerminalOutcome.FAILED_AFTER_EFFECT,
                TerminalOutcome.BLOCKED,
                TerminalOutcome.UNKNOWN,
            ):
                return

    def complete(
        self,
        request_id: str,
        result: Any,
        *,
        _expected_from: frozenset[str] | None = None,
        _expected_owner: str | None = None,
    ) -> LedgerEntry:
        existing = self._get_entry(request_id)
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
        if not self._try_transition(
            entry,
            expected_from=_expected_from,
            expected_owner=_expected_owner,
        ):
            current = self._get_entry(request_id)
            raise LedgerOutcomeAlreadySetError(
                f"Cannot complete request {request_id!r}: "
                f"terminal outcome already set to "
                f"{current.terminal_outcome if current else '?'} "
                f"(expected from {_expected_from or {'IN_FLIGHT'}})"
                + (
                    f", owner mismatch (expected {_expected_owner})"
                    if _expected_owner is not None
                    else ""
                )
            )
        return entry

    def fail(
        self,
        request_id: str,
        error: BaseException,
        *,
        failed_after_effect: bool = False,
        _expected_from: frozenset[str] | None = None,
        _expected_owner: str | None = None,
    ) -> LedgerEntry:
        existing = self._get_entry(request_id)
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
        if not self._try_transition(
            entry,
            expected_from=_expected_from,
            expected_owner=_expected_owner,
        ):
            current = self._get_entry(request_id)
            raise LedgerOutcomeAlreadySetError(
                f"Cannot fail request {request_id!r}: "
                f"terminal outcome already set to "
                f"{current.terminal_outcome if current else '?'} "
                f"(expected from {_expected_from or {'IN_FLIGHT'}}"
                + (
                    f", owner mismatch (expected {_expected_owner})"
                    if _expected_owner is not None
                    else ""
                )
            )
        return entry

    def attach_receipt_ref(self, request_id: str, receipt_ref: str) -> LedgerEntry:
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot attach receipt to unknown request {request_id!r}")
        entry = replace(existing, receipt_ref=receipt_ref)
        self._set_entry(entry)
        return entry

    def attach_external_operation_ref(
        self, request_id: str, ref: str
    ) -> LedgerEntry:
        """Store the provider's operation handle on a transition entry.

        Durable and used later for reconciliation. Backs
        :func:`record_external_operation`.
        """
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(
                f"Cannot attach external operation ref to unknown request {request_id!r}"
            )
        entry = replace(existing, external_operation_ref=ref)
        self._set_entry(entry)
        return entry

    def renew_lease(
        self,
        request_id: str,
        *,
        lease_ttl: float | None = None,
        now: float | None = None,
    ) -> LedgerEntry:
        """Extend ``lease_until`` for an in-flight transition.

        Owner-side heartbeat for long work: keeps peers on ``POLL`` instead of
        opening reclaim. This is the renew half of the ``REPAIR`` taxonomy
        (heal incomplete durable fields via :meth:`repair_transition`; extend a
        still-held lease here). Only applies while the stored terminal outcome
        is still ``IN_FLIGHT`` (before lease expiry is applied). Renewing after
        the lease has already expired raises :class:`LedgerError` — reclaim /
        reconcile must run instead of silently re-asserting ownership.

        Backs :func:`renew_lease`.
        """
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot renew lease for unknown request {request_id!r}")
        now = now if now is not None else time.time()
        stored = (
            existing.terminal_outcome
            if isinstance(existing.terminal_outcome, TerminalOutcome)
            else TerminalOutcome(str(existing.terminal_outcome))
        )
        if stored != TerminalOutcome.IN_FLIGHT:
            raise LedgerError(
                f"Cannot renew lease for request {request_id!r}: "
                f"terminal_outcome is {stored.value}, not IN_FLIGHT"
            )
        validity = resolve_lease_validity(existing.lease_until, now=now)
        if validity == LeaseValidity.EXPIRED:
            raise LedgerError(
                f"Cannot renew lease for request {request_id!r}: "
                "lease already expired — reclaim or reconcile instead"
            )
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        if ttl <= 0:
            raise LedgerError("lease_ttl must be positive to renew")
        entry = replace(existing, lease_until=now + ttl, last_heartbeat_at=now)
        self._set_entry(entry)
        return entry

    def repair_transition(self, request_id: str) -> LedgerEntry:
        """Heal incomplete durable transition fields before re-resolving.

        Fills missing ``idempotency_key`` / ``side_effect_boundary`` / terminal
        alignment. Does not renew a peer lease and does not execute the tool.
        Claim loops call this when the gate is ``REPAIR``, then re-resolve.
        """
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot repair unknown request {request_id!r}")
        updates = repair_transition_fields(existing)
        if not updates:
            if transition_needs_repair(existing):
                raise LedgerError(
                    f"Cannot repair request {request_id!r}: incomplete context "
                    "with no safe field updates"
                )
            return existing
        entry = replace(existing, **updates)
        if transition_needs_repair(entry):
            raise LedgerError(
                f"Cannot repair request {request_id!r}: still incomplete after "
                "safe field updates"
            )
        self._set_entry(entry)
        return entry

    def mark_blocked(
        self,
        request_id: str,
        *,
        error: str | None = None,
        _expected_from: frozenset[str] | None = None,
        _expected_owner: str | None = None,
    ) -> LedgerEntry:
        existing = self._get_entry(request_id)
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
        if not self._try_transition(
            entry,
            expected_from=_expected_from,
            expected_owner=_expected_owner,
        ):
            current = self._get_entry(request_id)
            raise LedgerOutcomeAlreadySetError(
                f"Cannot block request {request_id!r}: "
                f"terminal outcome already set to "
                f"{current.terminal_outcome if current else '?'}"
            )
        return entry

    def mark_unknown(
        self,
        request_id: str,
        *,
        error: str | None = None,
        _expected_from: frozenset[str] | None = None,
        _expected_owner: str | None = None,
    ) -> LedgerEntry:
        existing = self._get_entry(request_id)
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
        if not self._try_transition(
            entry,
            expected_from=_expected_from,
            expected_owner=_expected_owner,
        ):
            current = self._get_entry(request_id)
            raise LedgerOutcomeAlreadySetError(
                f"Cannot mark unknown request {request_id!r}: "
                f"terminal outcome already set to "
                f"{current.terminal_outcome if current else '?'}"
            )
        return entry

    def mark_worker_dead(
        self,
        owner: str,
        *,
        by: str,
        reason: str,
        now: float | None = None,
        override_heartbeat: bool = False,
    ) -> list[LedgerEntry]:
        """Assert that all transitions owned by *owner* are from a dead worker.

        Scans ``list_all()`` and stamps ``worker_dead_asserted_by`` /
        ``worker_dead_asserted_at`` on every entry whose ``owner`` matches and
        whose resolved outcome is ``IN_FLIGHT`` or ``EXPIRED``.  Entries whose
        ``last_heartbeat_at`` (falling back to ``started_at``) is within the
        grace window (``presumed_dead_after``) are **refused** — you cannot
        declare a currently-heartbeating worker dead.  Pass
        ``override_heartbeat=True`` to bypass this check when the operator has
        direct evidence of death (e.g. they killed the pod).  Bypassing may
        cause a duplicate effect if the worker is still alive.

        This is the channel for orchestrator events (k8s OOM-kill hooks,
        LangGraph redispatch sweeps) and humans.

        Returns the list of stamped entries (may be empty if no matching entries
        exist).
        """
        if not by:
            raise LedgerReleaseRefusedError(
                "mark_worker_dead requires an operator identity ('by')"
            )
        if not reason:
            raise LedgerReleaseRefusedError("mark_worker_dead requires a reason")
        now = now if now is not None else time.time()
        stamped: list[LedgerEntry] = []
        for entry in self._storage.list_all():
            if entry.owner != owner:
                continue
            resolved = entry.resolved_terminal_outcome(now=now)
            if resolved not in (TerminalOutcome.IN_FLIGHT, TerminalOutcome.EXPIRED):
                continue
            # Refuse if the worker appears alive (recent heartbeat).
            if not override_heartbeat and not has_worker_death_evidence(
                entry, now=now, presumed_dead_after=self._presumed_dead_after
            ):
                grace = _grace_remaining(
                    entry, now=now, presumed_dead_after=self._presumed_dead_after,
                )
                raise LedgerWorkerAliveError(
                    f"Cannot mark worker dead for owner {owner!r}: request "
                    f"{entry.request_id!r} has recent heartbeat "
                    f"({_format_heartbeat_age(entry, now=now)}) — "
                    f"grace window elapses in {grace}"
                )
            stored_reason = (
                f"{reason} (heartbeat overridden)" if override_heartbeat else reason
            )
            dead_entry = replace(
                entry,
                worker_dead_asserted_by=by,
                worker_dead_asserted_at=now,
                resolution_reason=stored_reason,
            )
            self._set_entry(dead_entry)
            stamped.append(dead_entry)
        return stamped

    def mark_worker_dead_for(
        self,
        request_id: str,
        *,
        by: str,
        reason: str,
        now: float | None = None,
        override_heartbeat: bool = False,
    ) -> LedgerEntry:
        """Assert that a specific transition's worker is dead.

        Per-entry variant of :meth:`mark_worker_dead`.  Stamps
        ``worker_dead_asserted_by`` / ``worker_dead_asserted_at`` on the named
        entry.  Refuses if the entry's ``last_heartbeat_at`` (or
        ``started_at`` fallback) is within the grace window
        (``presumed_dead_after``) **unless** ``override_heartbeat=True``.

        When ``override_heartbeat=True``, the liveness check is bypassed and
        ``" (heartbeat overridden)`` is appended to *reason* in the stored
        audit trail.  Use this only when the operator has direct evidence the
        worker is dead (e.g. they killed the pod themselves).  Bypassing the
        check may cause a duplicate effect if the worker is still alive.
        """
        if not by:
            raise LedgerReleaseRefusedError(
                "mark_worker_dead_for requires an operator identity ('by')"
            )
        if not reason:
            raise LedgerReleaseRefusedError("mark_worker_dead_for requires a reason")
        now = now if now is not None else time.time()
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot mark worker dead for unknown request {request_id!r}")
        resolved = existing.resolved_terminal_outcome(now=now)
        if resolved not in (TerminalOutcome.IN_FLIGHT, TerminalOutcome.EXPIRED):
            raise LedgerReleaseRefusedError(
                f"Cannot mark worker dead for request {request_id!r}: "
                f"resolved outcome is {resolved.value}, not IN_FLIGHT or EXPIRED"
            )
        if not override_heartbeat and not has_worker_death_evidence(
            existing, now=now, presumed_dead_after=self._presumed_dead_after
        ):
            grace = _grace_remaining(
                existing, now=now, presumed_dead_after=self._presumed_dead_after,
            )
            raise LedgerWorkerAliveError(
                f"Cannot mark worker dead for request {request_id!r}: "
                f"worker appears alive "
                f"({_format_heartbeat_age(entry=existing, now=now)}) — "
                f"grace window elapses in {grace}. "
                "Use --override-heartbeat if the operator has direct evidence "
                "of death (bypasses liveness check; may cause a duplicate "
                "effect if the worker is alive)."
            )
        stored_reason = (
            f"{reason} (heartbeat overridden)" if override_heartbeat else reason
        )
        entry = replace(
            existing,
            worker_dead_asserted_by=by,
            worker_dead_asserted_at=now,
            resolution_reason=stored_reason,
        )
        self._set_entry(entry)
        return entry

    def advance_boundary(
        self, request_id: str, boundary: SideEffectBoundary
    ) -> LedgerEntry:
        """Move an entry's side-effect boundary forward (monotonic).

        Only advances toward ``CROSSED`` and never regresses, so concurrent or
        out-of-order markers cannot weaken a stronger recorded boundary. Backs
        the :func:`side_effect` marker used by side-effecting tools.
        """
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(
                f"Cannot advance boundary for unknown request {request_id!r}"
            )
        current = SideEffectBoundary(existing.side_effect_boundary)
        if _BOUNDARY_RANK[boundary] <= _BOUNDARY_RANK[current]:
            return existing
        entry = replace(existing, side_effect_boundary=boundary.value)
        self._set_entry(entry)
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
        active_dispatch_id = get_active_dispatch_id()
        if active_dispatch_id is not None:
            return active_dispatch_id

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


def _claim_kwargs(kwargs: dict[str, Any], clean_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Kwargs for claim: tool args plus optional state-authority pass-through.

    ``state_ref`` / ``decision_id`` are bookkeeping (excluded from the tool body
    and args fingerprint) but must still reach ``_new_inflight_entry`` for audit.
    """
    claim_kwargs = dict(clean_kwargs)
    for key in ("decision_id", "state_ref"):
        if key in kwargs and kwargs[key] is not None:
            claim_kwargs[key] = kwargs[key]
    return claim_kwargs


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
        and transition_binding.side_effect_class == SideEffectClass.READ
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


def _record_failure(
    ledger: ActionLedger,
    request_id: str,
    exc: BaseException,
    *,
    _expected_owner: str | None = None,
) -> None:
    """Record a tool failure with the terminal outcome implied by the boundary.

    ``not_crossed`` → ``FAILED_BEFORE_EFFECT`` (safe to retry per policy),
    ``maybe_crossed`` → ``UNKNOWN`` (ambiguous; hard-block for reconcile),
    ``crossed`` → ``FAILED_AFTER_EFFECT`` (effect happened; hard-block).

    When *_expected_owner* is set, the write also fences on the stored entry's
    ``owner`` field (wrapper-path).
    """
    entry = ledger.get(request_id)
    boundary = (
        SideEffectBoundary(entry.side_effect_boundary)
        if entry is not None
        else SideEffectBoundary.NOT_CROSSED
    )
    if boundary == SideEffectBoundary.CROSSED:
        ledger.fail(
            request_id,
            exc,
            failed_after_effect=True,
            _expected_owner=_expected_owner,
        )
    elif boundary == SideEffectBoundary.MAYBE_CROSSED:
        ledger.mark_unknown(
            request_id,
            error=f"{type(exc).__name__}: {exc}",
            _expected_owner=_expected_owner,
        )
    else:
        ledger.fail(request_id, exc, _expected_owner=_expected_owner)


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
    claim_kwargs = _claim_kwargs(kwargs, clean_kwargs)
    _outcome_reexec_authorized.set(False)
    try:
        existing = _claim_for_transition(
            ledger,
            request_id,
            tool_name,
            args,
            claim_kwargs,
            transition_binding,
        )
    except LedgerHardBlockError:
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="resolution",
            gate="HARD_BLOCK",
            error_class="LedgerHardBlockError",
        )
        raise
    except LedgerSoftBlockError:
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="resolution",
            gate="SOFT_BLOCK",
            error_class="LedgerSoftBlockError",
        )
        raise
    if existing.is_terminal_completed():
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="resolution",
            gate="RETURN",
            terminal_outcome=TerminalOutcome.COMPLETED,
        )
        return existing.result

    owner = _ledger_owner()
    authorized_reexec = _outcome_reexec_authorized.get()
    side_effect_class = (
        transition_binding.side_effect_class
        if transition_binding is not None
        else None
    )
    ledger._emit_outcome(
        request_id=request_id,
        tool=tool_name,
        event="resolution",
        gate="ALLOW",
        terminal_outcome=TerminalOutcome.IN_FLIGHT,
        side_effect_class=side_effect_class,
        authorized_reexec=authorized_reexec,
        owner=owner,
    )
    ledger._emit_outcome(
        request_id=request_id,
        tool=tool_name,
        event="body_start",
        terminal_outcome=TerminalOutcome.IN_FLIGHT,
        side_effect_class=side_effect_class,
        tool_body_executed=True,
        authorized_reexec=authorized_reexec,
        owner=owner,
    )

    token = _active_transition_var.set(
        _ActiveTransition(ledger, request_id, transition_binding)
    )
    try:
        with _lease_auto_renew(ledger, request_id):
            result = func(*args, **clean_kwargs)
    except Exception as exc:
        # A storage failure while recording the failure must not mask the
        # original tool exception — log it, then re-raise the tool's own error.
        # An outcome-already-set error also does not mask — the transition was
        # resolved elsewhere after the tool started.
        try:
            _record_failure(ledger, request_id, exc, _expected_owner=owner)
            _emit_tool_receipt(audit_emitter, ledger, request_id)
        except LedgerOutcomeAlreadySetError:
            _logger.warning(
                "outcome already set for %s while recording failure "
                "(transition resolved elsewhere after tool started) — "
                "re-raising original exception",
                request_id,
            )
        except Exception:
            _logger.exception(
                "could not record failure for %s (storage down?); "
                "original tool error follows",
                request_id,
            )
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="body_fail",
            side_effect_class=side_effect_class,
            authorized_reexec=authorized_reexec,
            owner=owner,
            error_class=type(exc).__name__,
        )
        raise
    finally:
        _active_transition_var.reset(token)

    try:
        ledger.complete(request_id, result, _expected_owner=owner)
        complete_ok = True
    except LedgerOutcomeAlreadySetError:
        _logger.warning(
            "outcome already set for %s while completing "
            "(transition resolved elsewhere after tool started) — "
            "tool result discarded",
            request_id,
        )
        complete_ok = False
    _emit_tool_receipt(audit_emitter, ledger, request_id)
    ledger._emit_outcome(
        request_id=request_id,
        tool=tool_name,
        event="body_complete" if complete_ok else "body_fail",
        side_effect_class=side_effect_class,
        authorized_reexec=authorized_reexec,
        owner=owner,
        error_class=None if complete_ok else "LedgerOutcomeAlreadySetError",
    )
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
    claim_kwargs = _claim_kwargs(kwargs, clean_kwargs)
    _outcome_reexec_authorized.set(False)
    try:
        existing = await _claim_for_transition_async(
            ledger,
            request_id,
            tool_name,
            args,
            claim_kwargs,
            transition_binding,
        )
    except LedgerHardBlockError:
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="resolution",
            gate="HARD_BLOCK",
            error_class="LedgerHardBlockError",
        )
        raise
    except LedgerSoftBlockError:
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="resolution",
            gate="SOFT_BLOCK",
            error_class="LedgerSoftBlockError",
        )
        raise
    if existing.is_terminal_completed():
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="resolution",
            gate="RETURN",
            terminal_outcome=TerminalOutcome.COMPLETED,
        )
        return existing.result

    owner = _ledger_owner()
    authorized_reexec = _outcome_reexec_authorized.get()
    side_effect_class = (
        transition_binding.side_effect_class
        if transition_binding is not None
        else None
    )
    ledger._emit_outcome(
        request_id=request_id,
        tool=tool_name,
        event="resolution",
        gate="ALLOW",
        terminal_outcome=TerminalOutcome.IN_FLIGHT,
        side_effect_class=side_effect_class,
        authorized_reexec=authorized_reexec,
        owner=owner,
    )
    ledger._emit_outcome(
        request_id=request_id,
        tool=tool_name,
        event="body_start",
        terminal_outcome=TerminalOutcome.IN_FLIGHT,
        side_effect_class=side_effect_class,
        tool_body_executed=True,
        authorized_reexec=authorized_reexec,
        owner=owner,
    )

    token = _active_transition_var.set(
        _ActiveTransition(ledger, request_id, transition_binding)
    )
    try:
        with _lease_auto_renew(ledger, request_id):
            result = await func(*args, **clean_kwargs)
    except Exception as exc:
        # A storage failure while recording the failure must not mask the
        # original tool exception — log it, then re-raise the tool's own error.
        # An outcome-already-set error also does not mask — the transition was
        # resolved elsewhere after the tool started.
        try:
            _record_failure(ledger, request_id, exc, _expected_owner=owner)
            _emit_tool_receipt(audit_emitter, ledger, request_id)
        except LedgerOutcomeAlreadySetError:
            _logger.warning(
                "outcome already set for %s while recording failure "
                "(transition resolved elsewhere after tool started) — "
                "re-raising original exception",
                request_id,
            )
        except Exception:
            _logger.exception(
                "could not record failure for %s (storage down?); "
                "original tool error follows",
                request_id,
            )
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="body_fail",
            side_effect_class=side_effect_class,
            authorized_reexec=authorized_reexec,
            owner=owner,
            error_class=type(exc).__name__,
        )
        raise
    finally:
        _active_transition_var.reset(token)

    try:
        ledger.complete(request_id, result, _expected_owner=owner)
        complete_ok = True
    except LedgerOutcomeAlreadySetError:
        _logger.warning(
            "outcome already set for %s while completing "
            "(transition resolved elsewhere after tool started) — "
            "tool result discarded",
            request_id,
        )
        complete_ok = False
    _emit_tool_receipt(audit_emitter, ledger, request_id)
    ledger._emit_outcome(
        request_id=request_id,
        tool=tool_name,
        event="body_complete" if complete_ok else "body_fail",
        side_effect_class=side_effect_class,
        authorized_reexec=authorized_reexec,
        owner=owner,
        error_class=None if complete_ok else "LedgerOutcomeAlreadySetError",
    )
    return result


def _mark_ledgered(wrapper: Callable[..., Any], ledger: ActionLedger) -> None:
    wrapper._mycelium_ledger = True  # type: ignore[attr-defined]
    wrapper._mycelium_ledger_instance = ledger  # type: ignore[attr-defined]


def ledger(
    storage: LedgerStorage | None = None,
    audit_emitter: AuditReceiptEmitter | None = None,
    transition_binding: ToolTransitionBinding | None = None,
    *,
    outcome_emitter: OutcomeEmitter | None = None,
    lease_ttl: float | None = None,
    lease_renew_interval: float | None = None,
    poll_interval: float | None = None,
    poll_timeout: float | None = None,
    reconciler: Reconciler | None = None,
    defer_read_only_unknown: bool = False,
    unclassified_policy: str = UNCLASSIFIED_POLICY_WARN,
    reclaim_requires_death_signal: bool = False,
    presumed_dead_after: float | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that records async tool invocations in an ActionLedger.

    While the tool body runs, Mycelium auto-extends the execution lease
    (default every ``lease_ttl / 3``). Pass ``lease_renew_interval=0`` to
    disable; use :func:`renew_lease` for an extra manual bump.
    """

    ledger_kwargs: dict[str, float | bool | None] = {}
    if lease_ttl is not None:
        ledger_kwargs["lease_ttl"] = lease_ttl
    if lease_renew_interval is not None:
        ledger_kwargs["lease_renew_interval"] = lease_renew_interval
    if poll_interval is not None:
        ledger_kwargs["poll_interval"] = poll_interval
    if poll_timeout is not None:
        ledger_kwargs["poll_timeout"] = poll_timeout
    if reclaim_requires_death_signal:
        ledger_kwargs["reclaim_requires_death_signal"] = True
    if presumed_dead_after is not None:
        ledger_kwargs["presumed_dead_after"] = presumed_dead_after
    action_ledger = ActionLedger(
        storage=storage,
        reconciler=reconciler,
        defer_read_only_unknown=defer_read_only_unknown,
        audit_emitter=audit_emitter,
        outcome_emitter=outcome_emitter,
        unclassified_policy=unclassified_policy,
        **ledger_kwargs,
    )

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
    outcome_emitter: OutcomeEmitter | None = None,
    lease_ttl: float | None = None,
    lease_renew_interval: float | None = None,
    poll_interval: float | None = None,
    poll_timeout: float | None = None,
    reconciler: Reconciler | None = None,
    defer_read_only_unknown: bool = False,
    unclassified_policy: str = UNCLASSIFIED_POLICY_WARN,
    reclaim_requires_death_signal: bool = False,
    presumed_dead_after: float | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that records sync tool invocations in an ActionLedger.

    While the tool body runs, Mycelium auto-extends the execution lease
    (default every ``lease_ttl / 3``). Pass ``lease_renew_interval=0`` to
    disable; use :func:`renew_lease` for an extra manual bump.
    """

    ledger_kwargs: dict[str, float | bool | None] = {}
    if lease_ttl is not None:
        ledger_kwargs["lease_ttl"] = lease_ttl
    if lease_renew_interval is not None:
        ledger_kwargs["lease_renew_interval"] = lease_renew_interval
    if poll_interval is not None:
        ledger_kwargs["poll_interval"] = poll_interval
    if poll_timeout is not None:
        ledger_kwargs["poll_timeout"] = poll_timeout
    if reclaim_requires_death_signal:
        ledger_kwargs["reclaim_requires_death_signal"] = True
    if presumed_dead_after is not None:
        ledger_kwargs["presumed_dead_after"] = presumed_dead_after
    action_ledger = ActionLedger(
        storage=storage,
        reconciler=reconciler,
        defer_read_only_unknown=defer_read_only_unknown,
        audit_emitter=audit_emitter,
        outcome_emitter=outcome_emitter,
        unclassified_policy=unclassified_policy,
        **ledger_kwargs,
    )

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
    "DEFAULT_LEASE_RENEW_RATIO",
    "DEFAULT_LEASE_TTL",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_POLL_TIMEOUT",
    "DEFAULT_PRESUMED_DEAD_AFTER_RATIO",
    "OPERATOR_RESOLUTION_COMPLETED",
    "OPERATOR_RESOLUTION_NOT_EXECUTED",
    "UNCLASSIFIED_POLICY_WARN",
    "UNCLASSIFIED_POLICY_STRICT",
    "FileLedgerStorage",
    "InMemoryLedgerStorage",
    "LedgerAlreadyResolvedError",
    "LedgerEntry",
    "LedgerError",
    "LedgerHardBlockError",
    "LedgerPendingError",
    "LedgerPollTimeoutError",
    "LedgerReleaseRefusedError",
    "LedgerStorage",
    "LedgerStorageUnavailableError",
    "LedgerWorkerAliveError",
    "MIN_LEASE_RENEW_INTERVAL",
    "TerminalOutcome",
    "get_ledger",
    "ledger",
    "ledger_sync",
    "mark_crossed",
    "mark_maybe_crossed",
    "record_external_operation",
    "renew_lease",
    "side_effect",
]
