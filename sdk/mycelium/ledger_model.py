"""Ledger domain model: entries, exceptions, schema, and pure helpers.

This module owns the durable ledger row model and related constants/predicates.
It intentionally does not import concrete storage backends, decorators, or the
:mod:`mycelium.action_ledger` facade so it can be imported from both storage
and recovery code without cycles.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mycelium.transition import (
    EffectState,
    LeaseValidity,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    resolve_effect_state,
    resolve_lease_validity,
    resolve_terminal_outcome,
    terminal_from_legacy_status,
)

if TYPE_CHECKING:
    pass


__all__ = [
    "LedgerError",
    "LedgerSchemaVersionError",
    "LedgerPendingError",
    "LedgerPollTimeoutError",
    "LedgerHardBlockError",
    "LedgerSoftBlockError",
    "LedgerReleaseRefusedError",
    "LedgerAlreadyResolvedError",
    "LedgerOutcomeAlreadySetError",
    "LedgerWorkerAliveError",
    "LedgerStorageUnavailableError",
    "DEFAULT_LEASE_TTL",
    "DEFAULT_POLL_INTERVAL",
    "DEFAULT_POLL_TIMEOUT",
    "DEFAULT_LEASE_RENEW_RATIO",
    "MIN_LEASE_RENEW_INTERVAL",
    "DEFAULT_PRESUMED_DEAD_AFTER_RATIO",
    "OPERATOR_RESOLUTION_COMPLETED",
    "OPERATOR_RESOLUTION_NOT_EXECUTED",
    "UNCLASSIFIED_POLICY_WARN",
    "UNCLASSIFIED_POLICY_STRICT",
    "ARGS_DRIFT_OFF",
    "ARGS_DRIFT_SOFT",
    "ARGS_DRIFT_HARD",
    "ARGS_DRIFT_POLICIES",
    "LEDGER_ENTRY_SCHEMA_VERSION",
    "LedgerEntry",
    "_read_ledger_entry_schema_version",
    "_has_allowed_attempting_decision",
    "_UNCLASSIFIED_BINDING",
]


class LedgerError(Exception):
    """Raised when the action ledger cannot record or verify an action."""


class LedgerSchemaVersionError(LedgerError):
    """Raised when a durable ledger row uses an invalid or future schema."""


class LedgerPendingError(Exception):
    """Raised when the same request is already in-flight."""


class LedgerPollTimeoutError(LedgerError):
    """Raised when polling an in-flight transition times out."""


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


DEFAULT_LEASE_TTL = 3600.0
DEFAULT_POLL_INTERVAL = 0.05
DEFAULT_POLL_TIMEOUT = 300.0
# Renew at 1/3 of lease TTL so a still-running owner stays HELD before peers see EXPIRED.
DEFAULT_LEASE_RENEW_RATIO = 1.0 / 3.0
MIN_LEASE_RENEW_INTERVAL = 0.01
# Default grace window for worker-death evidence: 2x the lease TTL.
DEFAULT_PRESUMED_DEAD_AFTER_RATIO = 2.0


# Verified outcomes accepted by ActionLedger.release().
OPERATOR_RESOLUTION_COMPLETED = "completed"
OPERATOR_RESOLUTION_NOT_EXECUTED = "not_executed"

# Stored terminal-outcome values that resolution paths (release, reconcile)
# will accept from existing entries.  IN_FLIGHT (None) and COMPLETED are missing
# because resolution paths should never see them at write time.
_RESOLUTION_ACCEPTED_STORED_OUTCOMES: frozenset[str] = frozenset(
    {
        TerminalOutcome.IN_FLIGHT.value,
        TerminalOutcome.BLOCKED.value,
        TerminalOutcome.UNKNOWN.value,
        TerminalOutcome.FAILED_AFTER_EFFECT.value,
        TerminalOutcome.FAILED_BEFORE_EFFECT.value,
    }
)

# Expected terminal outcomes for a wrapper-path transition write.
_IN_FLIGHT_OUTCOMES: frozenset[str] = frozenset({TerminalOutcome.IN_FLIGHT.value})

# Stored terminal-outcome values that **the NOT_EXECUTED reset** accepts.
# Excludes ``IN_FLIGHT`` so two reconcilers racing ``NOT_EXECUTED``
# cannot both transition ``IN_FLIGHT → IN_FLIGHT`` — only the first
# writer wins; the second sees ``IN_FLIGHT`` and fails the CAS.
# EXPIRED entries (stored ``IN_FLIGHT`` with expired lease) are advanced
# to ``BLOCKED`` before the CAS (see ``_apply_reconcile_result``).
_RECONCILE_NOT_EXECUTED_OUTCOMES: frozenset[str] = frozenset(
    {
        TerminalOutcome.BLOCKED.value,
        TerminalOutcome.UNKNOWN.value,
        TerminalOutcome.FAILED_AFTER_EFFECT.value,
        TerminalOutcome.FAILED_BEFORE_EFFECT.value,
    }
)

# Opt-in same-key UNKNOWN retry (param + TTL still VALID). claim_inflight
# treats UNKNOWN as non-claimable so peers do not blind-overwrite; this CAS
# is the only authorized reset path after the gate returns ALLOW.
_UNKNOWN_SAME_KEY_RETRY_OUTCOMES: frozenset[str] = frozenset(
    {
        TerminalOutcome.UNKNOWN.value,
    }
)

# Policies for tools ledgered without a transition_binding (unclassified).
# "warn": legacy behavior + a one-time warning when a failed entry is
# reclaimed. "strict": route the claim through claim_side_effecting with a
# conservative synthesized binding so failed retries hard-block — this is
# the write-ahead-ordering-complete path (INTENDED -> ATTEMPTING/ABORTED CAS
# before any body execution), the same protocol every classified
# consequential tool goes through.
#
# ActionLedger.__init__ keeps "warn" as the constructor default for backward
# compatibility: every existing unclassified `claim()` caller (tests and
# hosts) that has not opted in would otherwise silently start hard-blocking
# failed retries. YAML tool templates already default to "strict" for new
# deployments (see mycelium/templates/); `mycelium doctor` accepts "warn" as
# a valid, non-erroring choice. Hosts that want every claim() — classified or
# not — to go through the full effect-commit protocol should pass
# `unclassified_policy="strict"` explicitly. `MyceliumConfig.apply_tool`
# additionally defaults this to "strict" when `profile: production` and
# `action_ledger.unclassified_policy` is omitted.
UNCLASSIFIED_POLICY_WARN = "warn"
UNCLASSIFIED_POLICY_STRICT = "strict"

# Opt-in identity-conflict / args-drift gate (AF-002 Ring 3). Default off
# preserves the intentional contract that same dispatch ticket + different
# args is a new transition (see test_semantic_identity).
ARGS_DRIFT_OFF = "off"
ARGS_DRIFT_SOFT = "soft"
ARGS_DRIFT_HARD = "hard"
ARGS_DRIFT_POLICIES = frozenset({ARGS_DRIFT_OFF, ARGS_DRIFT_SOFT, ARGS_DRIFT_HARD})

# Conservative binding synthesized for "strict" unclassified claims:
# NON_IDEMPOTENT_MUTATE yields MANUAL_RECONCILIATION_REQUIRED + SINGLE_USE
# from the existing class defaults. Request-id derivation stays legacy.
_UNCLASSIFIED_BINDING = ToolTransitionBinding.for_tool(
    agent_id="unclassified",
    policy_version="unclassified",
    side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
)


# LedgerEntry.schema_version. Bumped for the effect-commit protocol
# completion: rows now carry a durable `effect_id` (schema 2). Legacy rows
# missing the field load as schema 1 and infer `effect_id` from `request_id`
# (see LedgerEntry.from_dict) — this is a read-time inference, not a storage
# migration, so old rows keep working unchanged.
LEDGER_ENTRY_SCHEMA_VERSION = 2


def _read_ledger_entry_schema_version(data: Mapping[str, Any]) -> int:
    raw = data.get("schema_version", 1)
    if isinstance(raw, bool):
        raise LedgerSchemaVersionError("ledger schema_version must be an integer >= 1")
    if not isinstance(raw, (int, str)):
        raise LedgerSchemaVersionError(f"ledger schema_version must be an integer, got {raw!r}")
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise LedgerSchemaVersionError(
            f"ledger schema_version must be an integer, got {raw!r}"
        ) from exc
    if version < 1:
        raise LedgerSchemaVersionError(f"ledger schema_version must be >= 1, got {version}")
    if version > LEDGER_ENTRY_SCHEMA_VERSION:
        raise LedgerSchemaVersionError(
            f"ledger schema {version} is newer than this runtime supports "
            f"({LEDGER_ENTRY_SCHEMA_VERSION}); upgrade Mycelium before reading it"
        )
    return version


@dataclass(frozen=True)
class LedgerEntry:
    """Immutable record of a single tool invocation."""

    request_id: str
    tool: str
    args: list[Any]
    kwargs: dict[str, Any]
    status: str  # legacy: "in-flight" | "completed" | "failed"
    terminal_outcome: str = TerminalOutcome.IN_FLIGHT.value
    # Kleppmann fencing token. Every successful claim atomically bumps the
    # stored fence; the claimed entry carries it, and every later mutation must
    # match the stored fence or the storage CAS rejects the write. A worker
    # whose claim was superseded holds a stale (lower) fence and is refused at
    # the point of mutation — independent of its own lease clock. Old rows
    # without a fence load as 0.
    fence: int = 0
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

    # Durable record of the single-decision-point evaluation (Change 2). The
    # serialized :class:`mycelium.decision.Decision` — every registered
    # predicate's verdict — stamped atomically with the INTENDED -> ATTEMPTING
    # transition under the same fenced CAS. ``None`` when no decision was
    # recorded (timeless paths, older rows).
    decision: dict[str, Any] | None = None
    # Storage field for the unified WAL intent (mycelium.transition.EffectState).
    # Kept as ``effect_phase`` (not renamed to ``effect_state``) for
    # serialization compatibility with every existing stored row and every
    # existing raw-string comparison in this module; every value this field
    # holds is a member of EffectState. ``terminal_outcome`` remains the
    # legacy read alias (also carries UNKNOWN/BLOCKED/FAILED_* detail).
    # New protocol-gating code must not compare this field directly — call
    # resolved_effect_state() / resolve_effect_state(entry), which correctly
    # folds in UNKNOWN and legacy rows that predate this field.
    effect_phase: str = EffectState.INTENDED.value
    effect_protocol_required: bool = False

    # Stable effect identity (mycelium.transition.derive_effect_id_for_call):
    # deterministic hash of (scope, tool, canonicalized args/kwargs,
    # destination). This is the authoritative dedup identity for consequential
    # tools: storage backends maintain an effect_id -> canonical request_id
    # mapping and claim paths resolve through it before any side-effect write.
    # ``request_id`` remains the physical row key for backward compatibility.
    # Unclassified claim() rows still fall back to request_id.
    effect_id: str | None = None
    # Trusted release-scope metadata. Added without changing older row shape.
    tenant_id: str | None = None
    policy_version: str | None = None
    # Audit trail of host-supplied request ids that resolved onto this
    # canonical effect row via effect_id dedupe (includes request_id itself).
    request_id_aliases: tuple[str, ...] = ()
    # Schema version for this row's shape. See LEDGER_ENTRY_SCHEMA_VERSION.
    schema_version: int = LEDGER_ENTRY_SCHEMA_VERSION

    # Thin handoff / causation audit (optional). Set via ``handoff_scope`` or
    # kwargs; does not grant capabilities or change claim gates.
    parent_request_id: str | None = None
    handoff_id: str | None = None

    def __post_init__(self) -> None:
        # Match from_dict / claim: durable key defaults to request_id.
        if self.idempotency_key is None:
            object.__setattr__(self, "idempotency_key", self.request_id)
        # effect_id must always be present on a stored row (see field
        # docstring above); tools with no binding to derive it from (the
        # unclassified claim() path) fall back to request_id, same as
        # idempotency_key.
        if self.effect_id is None:
            object.__setattr__(self, "effect_id", self.request_id)
        aliases = tuple(
            str(item) for item in self.request_id_aliases if item is not None and str(item)
        )
        if self.request_id not in aliases:
            aliases = aliases + (self.request_id,)
        object.__setattr__(self, "request_id_aliases", aliases)

    def resolved_terminal_outcome(self, *, now: float | None = None) -> TerminalOutcome:
        return resolve_terminal_outcome(
            self.terminal_outcome,
            lease_until=self.lease_until,
            now=now,
        )

    def resolved_effect_state(self) -> EffectState:
        """Unified WAL intent (mycelium.transition.EffectState) for this row.

        The legacy-safe read path: works for rows written before this field
        existed as well as current rows. Prefer this (or
        :func:`mycelium.transition.resolve_effect_state`) over comparing
        ``effect_phase`` / ``terminal_outcome`` directly.
        """
        return resolve_effect_state(self)

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
            "fence": self.fence,
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
            "decision": self.decision,
            "effect_phase": self.effect_phase,
            "effect_protocol_required": self.effect_protocol_required,
            "effect_id": self.effect_id,
            "tenant_id": self.tenant_id,
            "policy_version": self.policy_version,
            "request_id_aliases": list(self.request_id_aliases),
            "schema_version": self.schema_version,
            "parent_request_id": self.parent_request_id,
            "handoff_id": self.handoff_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LedgerEntry:
        schema_version = _read_ledger_entry_schema_version(data)
        status = str(data["status"])
        lease_until = float(data["lease_until"]) if data.get("lease_until") is not None else None
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
            fence=int(data.get("fence") or 0),
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
            decision_id=(str(data["decision_id"]) if data.get("decision_id") is not None else None),
            state_ref=(str(data["state_ref"]) if data.get("state_ref") is not None else None),
            decision=(dict(data["decision"]) if data.get("decision") is not None else None),
            effect_phase=str(data.get("effect_phase") or EffectState.INTENDED.value),
            effect_protocol_required=bool(data.get("effect_protocol_required", False)),
            # Legacy rows (schema 1) have no effect_id: infer it from
            # request_id, which is exactly what it would equal for the
            # (default) derived-request_id path anyway.
            effect_id=str(data.get("effect_id") or request_id),
            tenant_id=(str(data["tenant_id"]) if data.get("tenant_id") is not None else None),
            policy_version=(
                str(data["policy_version"]) if data.get("policy_version") is not None else None
            ),
            request_id_aliases=tuple(
                str(item)
                for item in (data.get("request_id_aliases") or (request_id,))
                if item is not None and str(item)
            ),
            schema_version=schema_version,
            parent_request_id=(
                str(data["parent_request_id"])
                if data.get("parent_request_id") is not None
                else None
            ),
            handoff_id=(str(data["handoff_id"]) if data.get("handoff_id") is not None else None),
        )


def _has_allowed_attempting_decision(entry: LedgerEntry) -> bool:
    """True when an allowed decision was durably recorded at ATTEMPTING.

    Gates on the durable ``decision`` field alone, not on the current
    ``resolve_effect_state``: a row that crashed or was marked UNKNOWN while
    ATTEMPTING keeps its recorded allowed decision and must remain completable
    by the reconciler / operator. ``record_decision`` only stamps a decision
    during the ``INTENDED -> ATTEMPTING | ABORTED`` CAS, so a present, allowed
    decision provably means the row passed the single decision point.
    """
    if entry.decision is None:
        return False
    from mycelium.decision import Decision

    try:
        return Decision.from_dict(entry.decision).allowed
    except (KeyError, TypeError, ValueError):
        return False
