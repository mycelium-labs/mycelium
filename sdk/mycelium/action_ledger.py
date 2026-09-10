"""ActionLedger: durable action records and idempotency guard."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect  # noqa: F401  compatibility re-export
import json
import logging
import os  # noqa: F401  compatibility re-export
import socket  # noqa: F401  compatibility re-export
import threading  # noqa: F401  compatibility re-export
import time
import uuid
import warnings
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping  # noqa: F401
from contextlib import asynccontextmanager, contextmanager  # noqa: F401
from contextvars import ContextVar  # noqa: F401
from dataclasses import dataclass, replace  # noqa: F401
from types import SimpleNamespace  # noqa: F401  compatibility re-export
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from mycelium.ledger_storage import (
    FileLedgerStorage,
    InMemoryLedgerStorage,
    LedgerStorage,
    _storage_errors,
)
from mycelium.reconcile import (
    Reconciler,
    ReconcileResult,  # noqa: F401  compatibility re-export
    ReconcileStatus,  # noqa: F401  compatibility re-export
)
from mycelium.session import Session, _session_var
from mycelium.storage.transition_query import (
    TransitionPage,  # noqa: F401  compatibility re-export
    entry_sort_key,  # noqa: F401  compatibility re-export
)
from mycelium.tool_boundary import ToolBoundaryError
from mycelium.transition import (
    CONSEQUENTIAL_SIDE_EFFECT_CLASSES,
    LEDGER_KWARG_KEYS,  # noqa: F401  compatibility re-export
    REQUEST_IDENTITY_POLICIES,
    REQUEST_IDENTITY_POLICY_DERIVED,
    REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT,
    EffectState,
    LeaseValidity,
    MissingRequestIdentityError,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolCapability,  # noqa: F401  compatibility re-export
    ToolTransitionBinding,
    args_fingerprint,  # noqa: F401  compatibility re-export
    derive_dispatch_id,
    derive_effect_id_for_call,
    derive_transition_key_for_call,
    extract_provider_idempotency_key,
    get_active_dispatch_id,
    get_active_execution_scope,
    get_active_handoff,
    has_worker_death_evidence,
    legacy_status_from_terminal,
    parse_explicit_request_id,
    request_id_from_argument,
    resolve_lease_validity,
    resolve_scope,  # noqa: F401  compatibility re-export
    should_propagate_effect_id_as_provider_key,
)
from mycelium.transition_resolution import (
    TransitionGate,
    hard_block_message,
    repair_transition_fields,  # noqa: F401  compatibility re-export
    resolve_read_only_gate,
    resolve_side_effect_gate,
    soft_block_message,
    transition_needs_repair,  # noqa: F401  compatibility re-export
)

if TYPE_CHECKING:
    from mycelium.audit_receipt import AuditReceiptEmitter
    from mycelium.operator_auth import OperatorAuthorizer
    from mycelium.outcome_emit import OutcomeEmitter

from mycelium.ledger_context import (
    _active_transition_var,  # noqa: F401  compatibility re-export
    _ActiveTransition,  # noqa: F401  compatibility re-export
    _advance_active_boundary,  # noqa: F401  re-exported private helper
    _lease_auto_renew,  # noqa: F401  compatibility re-export
    _outcome_reexec_authorized,
    _reconcile_cas_lost,
    _resolve_lease_renew_interval,  # noqa: F401  re-exported private helper
    get_active_transition,  # noqa: F401  re-exported public helper
    mark_crossed,
    mark_maybe_crossed,
    mark_maybe_crossed_async,
    record_external_operation,
    renew_lease,
    side_effect,
    side_effect_async,
)
from mycelium.ledger_execution import (
    _ledger_owner,  # noqa: F401  compatibility re-export
    _run_ledgered,
    _run_ledgered_async,
)
from mycelium.ledger_identity import (
    _args_drift_exclude_keys,
    _args_drift_fingerprint,
    _args_drift_scope_key,
    _args_drift_scopes_match,
    _bind_args,
    _claim_kwargs,
    _drop_ledger_keys,
    _evidence_args,
    _evidence_error,
    _evidence_value,
    _identity_scopes_differ,
    _is_read_only_binding,  # noqa: F401  compatibility re-export
    _use_boundary_call_mapping,  # noqa: F401  compatibility re-export
)
from mycelium.ledger_model import (
    _IN_FLIGHT_OUTCOMES,
    _UNCLASSIFIED_BINDING,
    _UNKNOWN_SAME_KEY_RETRY_OUTCOMES,
    ARGS_DRIFT_HARD,
    ARGS_DRIFT_OFF,
    ARGS_DRIFT_POLICIES,
    ARGS_DRIFT_SOFT,
    DEFAULT_LEASE_RENEW_RATIO,
    DEFAULT_LEASE_TTL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_POLL_TIMEOUT,
    DEFAULT_PRESUMED_DEAD_AFTER_RATIO,
    LEDGER_ENTRY_SCHEMA_VERSION,  # noqa: F401  compatibility re-export
    MIN_LEASE_RENEW_INTERVAL,
    OPERATOR_RESOLUTION_COMPLETED,
    OPERATOR_RESOLUTION_NOT_EXECUTED,
    UNCLASSIFIED_POLICY_STRICT,
    UNCLASSIFIED_POLICY_WARN,
    LedgerAlreadyResolvedError,
    LedgerEntry,
    LedgerError,
    LedgerHardBlockError,
    LedgerOutcomeAlreadySetError,
    LedgerPendingError,
    LedgerPollTimeoutError,
    LedgerReleaseRefusedError,
    LedgerSchemaVersionError,  # noqa: F401  compatibility re-export
    LedgerSoftBlockError,
    LedgerStorageUnavailableError,
    LedgerWorkerAliveError,
    _has_allowed_attempting_decision,
)
from mycelium.ledger_recovery import (
    LedgerRecoveryMixin,
    _format_heartbeat_age,  # noqa: F401  compatibility re-export
    _grace_remaining,  # noqa: F401  compatibility re-export
    _is_stuck_transition,  # noqa: F401  compatibility re-export
)

P = ParamSpec("P")
R = TypeVar("R")

_logger = logging.getLogger(__name__)




# Boundary ordering: a transition may only move forward toward CROSSED.
_BOUNDARY_RANK: dict[SideEffectBoundary, int] = {
    SideEffectBoundary.NOT_CROSSED: 0,
    SideEffectBoundary.MAYBE_CROSSED: 1,
    SideEffectBoundary.CROSSED: 2,
}

class ActionLedger(LedgerRecoveryMixin):
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
        operator_authorizer: OperatorAuthorizer | None = None,
        unclassified_policy: str = UNCLASSIFIED_POLICY_WARN,
        on_args_drift: str = ARGS_DRIFT_SOFT,
        reclaim_requires_death_signal: bool = False,
        presumed_dead_after: float | None = None,
        request_identity_policy: str = REQUEST_IDENTITY_POLICY_DERIVED,
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
        # Optional host policy for authenticating and authorizing releases.
        # None preserves the documented legacy honesty model.
        self._operator_authorizer = operator_authorizer
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
        if on_args_drift not in ARGS_DRIFT_POLICIES:
            raise ValueError(
                f"on_args_drift must be one of {sorted(ARGS_DRIFT_POLICIES)}, got {on_args_drift!r}"
            )
        # Default soft: same dispatch ticket (request_id / tool_call_id) with
        # different tool args → ToolBoundaryError (hard → LedgerHardBlockError;
        # off restores the old "new args = new transition" escape hatch).
        # Default off: same ticket + different args remains a new transition.
        self._on_args_drift = on_args_drift
        self._memory_warned_tools: set[str] = set()
        self._unclassified_warned_tools: set[str] = set()
        # Worker-death signal: when True, EXPIRED entries cannot be reclaimed
        # without affirmative death evidence (mark_worker_dead or heartbeat
        # older than presumed_dead_after). Default False for backward compat;
        # mycelium init scaffolds True — enable in production.
        self._reclaim_requires_death_signal = reclaim_requires_death_signal
        # Grace window: seconds since last heartbeat (or started_at) after
        # which a worker is presumed dead. Default 2x lease_ttl.
        self._presumed_dead_after = (
            presumed_dead_after
            if presumed_dead_after is not None
            else lease_ttl * DEFAULT_PRESUMED_DEAD_AFTER_RATIO
        )
        if request_identity_policy not in REQUEST_IDENTITY_POLICIES:
            raise ValueError(
                "request_identity_policy must be "
                f"{REQUEST_IDENTITY_POLICY_DERIVED!r} or "
                f"{REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT!r}, "
                f"got {request_identity_policy!r}"
            )
        self._request_identity_policy = request_identity_policy

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
        require_lease_held_at: float | None = None,
        expected_fence: int | None = None,
        expected_effect_state: str | None = None,
    ) -> bool:
        """Atomically write *entry* subject to outcome/owner/fence pre-conditions.

        Returns ``True`` on success, ``False`` when the stored entry's
        terminal outcome is not in *expected_from* (or owner / fence mismatch).
        The caller raises ``LedgerOutcomeAlreadySetError`` on ``False``.
        """
        outcomes = expected_from if expected_from is not None else _IN_FLIGHT_OUTCOMES
        fence = entry.fence if expected_fence is None else expected_fence
        with _storage_errors("try_transition"):
            return self._storage.try_transition(
                entry,
                expected_terminal_outcomes=outcomes,
                expected_owner=expected_owner,
                require_lease_held_at=require_lease_held_at,
                expected_fence=fence,
                expected_effect_state=expected_effect_state,
            )

    def _list_all_entries(self) -> list[LedgerEntry]:
        with _storage_errors("list_all"):
            return self._storage.list_all()

    def _resolve_request_id_for_effect(self, effect_id: str) -> str | None:
        with _storage_errors("resolve_request_id"):
            return self._storage.resolve_request_id(effect_id)

    def _get_entry_by_effect_id(self, effect_id: str) -> LedgerEntry | None:
        with _storage_errors("get_by_effect_id"):
            return self._storage.get_by_effect_id(effect_id)

    def _enforce_args_drift(
        self,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        request_id: str,
        existing: LedgerEntry | None,
        binding: ToolTransitionBinding | None = None,
        incoming_request_id: str | None = None,
    ) -> None:
        """Block when the same dispatch ticket is reused with different args.

        Default ``on_args_drift="soft"`` refuses the second body (pitch:
        corrupted upstream args must not double-execute). ``off`` is an
        explicit escape hatch; ``hard`` freezes for a human.

        1. Same storage key (``request_id``) with a prior entry whose
           ``args_fingerprint`` differs → conflict.
        2. Same ``tool_call_id`` / ``request_id`` dispatch ticket under a
           *different* transition key (args are in the key) → conflict,
           but only within the same run isolation scope (``run_id``, else
           ``thread_id``). Other runs are ignored.

        Provider idempotency-key kwargs are excluded from the fingerprint
        (same as transition-key derivation) so the dedicated provider-key
        gate can still hard-block a key mismatch.

        Soft raises :class:`ToolBoundaryError`; hard raises
        :class:`LedgerHardBlockError`.

        An explicit host ``request_id`` is the transition identity. Reusing it
        with a different tool, scope, or meaningful arguments is always
        fail-closed (``off`` does not dual-execute that ticket).
        """
        exclude = _args_drift_exclude_keys(binding)
        incoming_fp = _args_drift_fingerprint(args, kwargs, exclude=exclude)
        conflict: LedgerEntry | None = None
        explicit = None
        try:
            explicit = parse_explicit_request_id(kwargs)
        except ValueError:
            explicit = None

        alias_redispatch = (
            incoming_request_id is not None
            and incoming_request_id != request_id
            and existing is not None
        )
        if existing is not None:
            stored_fp = _args_drift_fingerprint(
                tuple(existing.args), dict(existing.kwargs), exclude=exclude
            )
            if alias_redispatch:
                alias_kwargs = {
                    key: value for key, value in kwargs.items() if key != "request_id"
                }
                alias_fp = _args_drift_fingerprint(args, alias_kwargs, exclude=exclude)
                stored_alias_kwargs = {
                    key: value
                    for key, value in dict(existing.kwargs).items()
                    if key != "request_id"
                }
                stored_alias_fp = _args_drift_fingerprint(
                    tuple(existing.args), stored_alias_kwargs, exclude=exclude
                )
                if (
                    existing.tool != tool
                    or (
                        not alias_redispatch
                        and _identity_scopes_differ(existing, kwargs, binding)
                    )
                    or stored_alias_fp != alias_fp
                ):
                    self._raise_identity_conflict(
                        tool,
                        request_id=incoming_request_id,
                        conflict=existing,
                    )
            elif explicit is not None and (
                existing.tool != tool
                or _identity_scopes_differ(existing, kwargs, binding)
                or stored_fp != incoming_fp
            ):
                # Host-owned request_id is the identity: mismatch is
                # fail-closed even when on_args_drift is off.
                self._raise_identity_conflict(tool, request_id=request_id, conflict=existing)
            elif stored_fp != incoming_fp:
                conflict = existing

        if self._on_args_drift == ARGS_DRIFT_OFF:
            return

        if conflict is None:
            dispatch_id = derive_dispatch_id(kwargs)
            if dispatch_id is not None:
                incoming_scope = _args_drift_scope_key(kwargs)
                for entry in self._list_all_entries():
                    if entry.request_id == request_id:
                        continue
                    if entry.tool != tool:
                        continue
                    entry_kwargs = dict(entry.kwargs)
                    if not _args_drift_scopes_match(
                        incoming_scope, _args_drift_scope_key(entry_kwargs)
                    ):
                        continue
                    entry_dispatch = derive_dispatch_id(entry_kwargs)
                    if entry_dispatch != dispatch_id:
                        continue
                    stored_fp = _args_drift_fingerprint(
                        tuple(entry.args), entry_kwargs, exclude=exclude
                    )
                    if stored_fp != incoming_fp:
                        conflict = entry
                        break

        if conflict is None:
            return

        self._raise_identity_conflict(tool, request_id=request_id, conflict=conflict)

    def _raise_identity_conflict(
        self,
        tool: str,
        *,
        request_id: str,
        conflict: LedgerEntry,
    ) -> None:
        message = (
            f"Args drift / identity conflict for tool {tool!r}: dispatch ticket "
            f"already recorded with a different tool, scope, or arguments "
            f"(prior request_id={conflict.request_id!r}, "
            f"incoming request_id={request_id!r}, prior tool={conflict.tool!r}). "
            f"Mint a new request_id / tool_call_id for a genuinely new intent."
        )
        if self._on_args_drift == ARGS_DRIFT_HARD:
            raise LedgerHardBlockError(message)
        raise ToolBoundaryError(
            message,
            violation="args_drift",
            tool_name=tool,
            llm_message=(
                f"Identity conflict: {tool!r} was already claimed with a different "
                "tool, scope, or arguments for this dispatch ticket. Mint a new "
                "request_id / tool_call_id for a new intent, or reuse the original "
                "identity. The tool body was not executed."
            ),
            recovery_hint=(
                "Reuse the original tool, scope, and arguments for this ticket, "
                "or issue a new request_id / tool_call_id."
            ),
        )

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
        policy_version: str | None = None,
    ) -> None:
        """Emit one outcome row, backfilling state from the stored entry.

        Fail-closed emitters re-raise; warn-mode emitters log and swallow
        so telemetry cannot alter claim/CAS/reconcile semantics.
        """
        if self._outcome_emitter is None:
            return
        try:
            entry = self.get(request_id)
        except Exception:
            entry = None
        run_id: str | None = None
        external_operation_ref: str | None = None
        resolution_reason: str | None = None
        parent_request_id: str | None = None
        handoff_id: str | None = None
        if entry is not None:
            if terminal_outcome is None:
                terminal_outcome = entry.resolved_terminal_outcome()
            if boundary is None:
                boundary = SideEffectBoundary(entry.side_effect_boundary)
            stored_kwargs = dict(entry.kwargs or {})
            raw_run = stored_kwargs.get("run_id")
            run_id = str(raw_run) if raw_run else None
            external_operation_ref = entry.external_operation_ref
            resolution_reason = entry.resolution_reason
            parent_request_id = entry.parent_request_id
            handoff_id = entry.handoff_id
        if not run_id:
            scope = get_active_execution_scope()
            if scope is not None and scope.run_id:
                run_id = scope.run_id
        try:
            self._outcome_emitter.emit_event(
                tool=tool,
                request_id=request_id,
                event=event,
                gate=gate,
                terminal_outcome=(terminal_outcome.value if terminal_outcome is not None else None),
                side_effect_boundary=boundary.value if boundary is not None else None,
                side_effect_class=(
                    side_effect_class.value if side_effect_class is not None else None
                ),
                tool_body_executed=tool_body_executed,
                dispatch_attempt=dispatch_attempt,
                authorized_reexec=authorized_reexec,
                owner=owner,
                error_class=error_class,
                run_id=run_id,
                policy_version=policy_version,
                external_operation_ref=external_operation_ref,
                resolution_reason=resolution_reason,
                parent_request_id=parent_request_id,
                handoff_id=handoff_id,
            )
        except Exception:
            if getattr(self._outcome_emitter, "fail_closed", False):
                raise
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

    def _new_inflight_entry(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        binding: ToolTransitionBinding | None = None,
        _provider_key_first_attempt_at: float | None = None,
        _provider_idempotency_key: str | None = None,
        _effect_id: str | None = None,
    ) -> LedgerEntry:
        bound = _bind_args(args, kwargs)
        boundary = (
            binding.side_effect_boundary_default.value
            if binding is not None
            else SideEffectBoundary.NOT_CROSSED.value
        )
        provider_key = _provider_idempotency_key
        if provider_key is None and binding is not None:
            provider_key = extract_provider_idempotency_key(kwargs, binding)
        if provider_key is not None and _provider_key_first_attempt_at is None:
            pkey_first_attempt: float | None = time.time()
        else:
            pkey_first_attempt = _provider_key_first_attempt_at
        decision_raw = kwargs.get("decision_id")
        state_ref_raw = kwargs.get("state_ref")
        parent_raw = kwargs.get("parent_request_id")
        handoff_raw = kwargs.get("handoff_id")
        active_handoff = get_active_handoff()
        if parent_raw is None and active_handoff is not None:
            parent_raw = active_handoff.parent_request_id
        if handoff_raw is None and active_handoff is not None:
            handoff_raw = active_handoff.handoff_id
        stored_args, stored_kwargs = _evidence_args(bound["args"], bound["kwargs"])
        # Stable effect identity, present whenever a binding is available to
        # derive it from (classified tools only — unclassified claim() has no
        # side-effect class and stays effect_id=None). Same derivation as
        # derive_request_id's fallback, so request_id == effect_id whenever
        # request_id itself was derived (the default) rather than explicit.
        effect_id = (
            _effect_id
            if _effect_id is not None
            else (
                derive_effect_id_for_call(tool, args, kwargs, binding)
                if binding is not None
                else None
            )
        )
        return LedgerEntry(
            request_id=request_id,
            tool=tool,
            args=stored_args,
            kwargs=stored_kwargs,
            status=legacy_status_from_terminal(TerminalOutcome.IN_FLIGHT),
            terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
            owner=_ledger_owner(),
            idempotency_key=request_id,
            side_effect_boundary=boundary,
            provider_idempotency_key=provider_key,
            provider_key_first_attempt_at=pkey_first_attempt,
            decision_id=str(decision_raw) if decision_raw is not None else None,
            state_ref=str(state_ref_raw) if state_ref_raw is not None else None,
            parent_request_id=str(parent_raw) if parent_raw is not None else None,
            handoff_id=str(handoff_raw) if handoff_raw is not None else None,
            effect_protocol_required=binding is not None,
            effect_id=effect_id,
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
        prior = self._get_entry(request_id)
        self._enforce_args_drift(tool, args, kwargs, request_id=request_id, existing=prior)
        self._warn_unclassified_retry(tool, prior)
        entry = self._new_inflight_entry(request_id, tool, args, kwargs)
        outcome, existing = self._try_claim_inflight(entry, lease_ttl=ttl)
        if outcome == "completed" and existing is not None:
            self._enforce_args_drift(tool, args, kwargs, request_id=request_id, existing=existing)
            return existing
        if outcome == "in_flight":
            raise LedgerPendingError(f"Tool {tool!r} request {request_id!r} is already in-flight")
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
            self._enforce_args_drift(tool, args, kwargs, request_id=request_id, existing=existing)
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
                self._enforce_args_drift(
                    tool, args, kwargs, request_id=request_id, existing=existing
                )
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
            raise LedgerError(f"Unexpected claim outcome {outcome!r} for read-only tool {tool!r}")

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
        fresh = replace(fresh, fence=existing.fence + 1)
        if not self._try_transition(
            fresh,
            expected_from=frozenset({existing.terminal_outcome}),
            expected_owner=existing.owner,
            expected_fence=existing.fence,
        ):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot retry read-only request {request_id!r}: transition superseded"
            )
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
                raise LedgerPollTimeoutError(f"Timed out polling read-only request {request_id!r}")
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
            self._enforce_args_drift(tool, args, kwargs, request_id=request_id, existing=existing)
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
                self._enforce_args_drift(
                    tool, args, kwargs, request_id=request_id, existing=existing
                )
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
            raise LedgerError(f"Unexpected claim outcome {outcome!r} for read-only tool {tool!r}")

    async def _poll_read_only_async(
        self,
        request_id: str,
        *,
        interval: float,
        poll_deadline: float | None,
    ) -> None:
        while True:
            if poll_deadline is not None and time.time() >= poll_deadline:
                raise LedgerPollTimeoutError(f"Timed out polling read-only request {request_id!r}")
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

    def _reset_unknown_for_same_key_retry(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        existing: LedgerEntry,
        binding: ToolTransitionBinding,
        *,
        lease_ttl: float,
    ) -> LedgerEntry | None:
        """CAS-reset ``UNKNOWN`` → fresh in-flight for opt-in same-key retry.

        ``try_claim_inflight`` refuses to overwrite ``UNKNOWN`` (fail-closed for
        peers). After the gate has ALLOW'd within the provider key window, this
        is the authorized transition — same shape as Reconciler ``NOT_EXECUTED``.

        A BLIND tool (or a QUERYABLE tool with no reconciler) never opts into
        same-key retry even with a valid provider key + TTL: BLIND declaration
        wins, so it parks for operator reconciliation instead.
        """
        if self._blind_never_retries(tool, binding, existing):
            return None
        pkey_first = (
            existing.provider_key_first_attempt_at
            if existing.provider_idempotency_key is not None
            else None
        )
        explicit_provider_key = extract_provider_idempotency_key(kwargs, binding)
        fresh = self._new_inflight_entry(
            request_id,
            tool,
            args,
            kwargs,
            binding=binding,
            _provider_key_first_attempt_at=pkey_first,
            _provider_idempotency_key=(
                explicit_provider_key
                if explicit_provider_key is not None
                else existing.provider_idempotency_key
            ),
        )
        now = time.time()
        fresh = replace(
            fresh,
            fence=existing.fence + 1,
            lease_until=(now + lease_ttl if lease_ttl > 0 else None),
            last_heartbeat_at=now,
        )
        if not self._try_transition(
            fresh,
            expected_from=_UNKNOWN_SAME_KEY_RETRY_OUTCOMES,
            expected_owner=existing.owner,
            expected_fence=existing.fence,
        ):
            return None
        _outcome_reexec_authorized.set(True)
        return fresh

    def _record_request_id_alias(self, canonical_request_id: str, supplied_request_id: str) -> None:
        """Best-effort audit stamp for explicit request-id aliases."""
        if canonical_request_id == supplied_request_id:
            return
        existing = self.get(canonical_request_id)
        if existing is None:
            return
        if supplied_request_id in existing.request_id_aliases:
            return
        updated = replace(
            existing,
            request_id_aliases=existing.request_id_aliases + (supplied_request_id,),
        )
        self._try_transition(
            updated,
            expected_from=frozenset({existing.terminal_outcome}),
            expected_owner=existing.owner,
            expected_fence=existing.fence,
        )

    def _canonical_request_id_for_effect(
        self,
        *,
        effect_id: str,
        request_id: str,
    ) -> str:
        canonical = self._resolve_request_id_for_effect(effect_id)
        if canonical is None:
            return request_id
        if canonical != request_id:
            self._record_request_id_alias(canonical, request_id)
        return canonical

    @staticmethod
    def _effective_incoming_provider_key(
        *,
        binding: ToolTransitionBinding,
        kwargs: dict[str, Any],
        effect_id: str,
        existing: LedgerEntry | None,
    ) -> str | None:
        incoming = extract_provider_idempotency_key(kwargs, binding)
        if incoming is not None:
            return incoming
        if not should_propagate_effect_id_as_provider_key(binding):
            return None
        if existing is not None and existing.provider_idempotency_key is not None:
            return existing.provider_idempotency_key
        return effect_id

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
        _effect_id: str | None = None,
    ) -> LedgerEntry:
        """Claim or resolve a side-effecting tool transition.

        ``_effect_id`` is an internal adapter hook for protocol engines that
        already derived identity outside the legacy Python serializer. The
        default preserves existing wrapper behavior.
        """
        self._warn_if_volatile_side_effect_storage(tool, binding)
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        interval = self._poll_interval if poll_interval is None else poll_interval
        timeout = self._poll_timeout if poll_timeout is None else poll_timeout
        poll_deadline = time.time() + timeout if timeout is not None else None
        if _effect_id is not None:
            if (
                not _effect_id.startswith("mycelium:effect:v1:")
                or len(_effect_id) != len("mycelium:effect:v1:") + 64
                or any(char not in "0123456789abcdef" for char in _effect_id.split(":")[-1])
            ):
                raise LedgerError("invalid protocol effect identity")
        effect_id = (
            _effect_id
            if _effect_id is not None
            else derive_effect_id_for_call(tool, args, kwargs, binding)
        )

        while True:
            claim_kwargs = _claim_kwargs(dict(kwargs), _drop_ledger_keys(dict(kwargs)))
            canonical_request_id = self._canonical_request_id_for_effect(
                effect_id=effect_id,
                request_id=request_id,
            )
            existing = self.get(canonical_request_id)
            explicit_provider_key = extract_provider_idempotency_key(kwargs, binding)
            incoming_key = self._effective_incoming_provider_key(
                binding=binding,
                kwargs=kwargs,
                effect_id=effect_id,
                existing=existing,
            )
            self._enforce_args_drift(
                tool,
                args,
                claim_kwargs,
                request_id=canonical_request_id,
                existing=existing,
                binding=binding,
                incoming_request_id=request_id,
            )
            if existing is not None:
                gate = resolve_side_effect_gate(
                    existing,
                    binding,
                    incoming_provider_idempotency_key=incoming_key,
                )
                if gate == TransitionGate.REPAIR:
                    self.repair_transition(canonical_request_id)
                    continue
                if gate == TransitionGate.RETURN:
                    return self.get(canonical_request_id) or existing
                if gate == TransitionGate.HARD_BLOCK:
                    entry = self._reconcile_or_hard_block(
                        canonical_request_id, tool, args, kwargs, existing, binding
                    )
                    if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                        if getattr(_reconcile_cas_lost, "val", False):
                            _reconcile_cas_lost.val = False
                            self._poll_side_effecting(
                                canonical_request_id,
                                tool=tool,
                                interval=interval,
                                poll_deadline=poll_deadline,
                            )
                            continue
                    return entry
                if gate == TransitionGate.POLL:
                    self._poll_side_effecting(
                        canonical_request_id,
                        tool=tool,
                        interval=interval,
                        poll_deadline=poll_deadline,
                    )
                    continue
                if gate == TransitionGate.ALLOW:
                    settled = self._prefer_settle_before_unknown_allow(
                        canonical_request_id, tool, args, kwargs, existing, binding
                    )
                    if settled is not None:
                        return settled
                    if existing.resolved_terminal_outcome() == TerminalOutcome.UNKNOWN:
                        reset = self._reset_unknown_for_same_key_retry(
                            canonical_request_id,
                            tool,
                            args,
                            kwargs,
                            existing,
                            binding,
                            lease_ttl=ttl,
                        )
                        if reset is not None:
                            return reset
                        if self._blind_never_retries(tool, binding, existing):
                            return self._raise_hard_block(
                                canonical_request_id, tool, existing, binding=binding
                            )
                        continue
                    if self._blind_never_retries(tool, binding, existing):
                        return self._raise_hard_block(
                            canonical_request_id, tool, existing, binding=binding
                        )
                    if self._reclaim_requires_death_signal and not has_worker_death_evidence(
                        existing,
                        now=time.time(),
                        presumed_dead_after=self._presumed_dead_after,
                    ):
                        self._poll_side_effecting(
                            canonical_request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue

            _old_pkey_attempt = (
                existing.provider_key_first_attempt_at
                if existing is not None and existing.provider_idempotency_key is not None
                else None
            )
            entry = self._new_inflight_entry(
                canonical_request_id,
                tool,
                args,
                claim_kwargs,
                binding=binding,
                _provider_key_first_attempt_at=_old_pkey_attempt,
                _provider_idempotency_key=(
                    explicit_provider_key
                    if explicit_provider_key is not None
                    else (
                        existing.provider_idempotency_key
                        if existing is not None
                        else None
                    )
                ),
                _effect_id=effect_id,
            )
            outcome, existing = self._try_claim_inflight(entry, lease_ttl=ttl)
            if outcome == "completed" and existing is not None:
                return existing
            if outcome == "in_flight" and existing is not None:
                canonical_request_id = existing.request_id
                incoming_key = self._effective_incoming_provider_key(
                    binding=binding,
                    kwargs=kwargs,
                    effect_id=effect_id,
                    existing=existing,
                )
                gate = resolve_side_effect_gate(
                    existing,
                    binding,
                    incoming_provider_idempotency_key=incoming_key,
                )
                if gate == TransitionGate.REPAIR:
                    self.repair_transition(canonical_request_id)
                    continue
                if gate == TransitionGate.RETURN:
                    return existing
                if gate == TransitionGate.HARD_BLOCK:
                    entry = self._reconcile_or_hard_block(
                        canonical_request_id, tool, args, kwargs, existing, binding
                    )
                    if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                        if getattr(_reconcile_cas_lost, "val", False):
                            _reconcile_cas_lost.val = False
                            self._poll_side_effecting(
                                canonical_request_id,
                                tool=tool,
                                interval=interval,
                                poll_deadline=poll_deadline,
                            )
                            continue
                    return entry
                if gate == TransitionGate.ALLOW and self._blind_never_retries(
                    tool, binding, existing
                ):
                    self._poll_side_effecting(
                        canonical_request_id,
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
                            canonical_request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue
                self._poll_side_effecting(
                    canonical_request_id,
                    tool=tool,
                    interval=interval,
                    poll_deadline=poll_deadline,
                )
                continue
            if outcome == "claimed":
                claimed = self.get(canonical_request_id)
                return claimed if claimed is not None else entry
            if existing is not None:
                canonical_request_id = existing.request_id
                entry = self._reconcile_or_hard_block(
                    canonical_request_id, tool, args, kwargs, existing, binding
                )
                if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                    if getattr(_reconcile_cas_lost, "val", False):
                        _reconcile_cas_lost.val = False
                        self._poll_side_effecting(
                            canonical_request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue
                    return entry
                return entry
            raise LedgerError(
                f"Unexpected claim outcome {outcome!r} for side-effecting tool {tool!r} "
                f"(request_id={canonical_request_id!r})"
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
                    try:
                        self.mark_unknown(
                            request_id,
                            error="timed out polling in-flight side-effecting transition",
                            _expected_from=_IN_FLIGHT_OUTCOMES,
                            _expected_owner=current.owner,
                            _expected_fence=current.fence,
                        )
                    except LedgerOutcomeAlreadySetError:
                        return
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
        _effect_id: str | None = None,
    ) -> LedgerEntry:
        """Async variant of :meth:`claim_side_effecting`."""
        self._warn_if_volatile_side_effect_storage(tool, binding)
        ttl = self._lease_ttl if lease_ttl is None else lease_ttl
        interval = self._poll_interval if poll_interval is None else poll_interval
        timeout = self._poll_timeout if poll_timeout is None else poll_timeout
        poll_deadline = time.time() + timeout if timeout is not None else None
        effect_id = _effect_id or derive_effect_id_for_call(tool, args, kwargs, binding)

        while True:
            claim_kwargs = _claim_kwargs(dict(kwargs), _drop_ledger_keys(dict(kwargs)))
            canonical_request_id = self._canonical_request_id_for_effect(
                effect_id=effect_id,
                request_id=request_id,
            )
            existing = self.get(canonical_request_id)
            explicit_provider_key = extract_provider_idempotency_key(kwargs, binding)
            incoming_key = self._effective_incoming_provider_key(
                binding=binding,
                kwargs=kwargs,
                effect_id=effect_id,
                existing=existing,
            )
            self._enforce_args_drift(
                tool,
                args,
                claim_kwargs,
                request_id=canonical_request_id,
                existing=existing,
                binding=binding,
                incoming_request_id=request_id,
            )
            if existing is not None:
                gate = resolve_side_effect_gate(
                    existing,
                    binding,
                    incoming_provider_idempotency_key=incoming_key,
                )
                if gate == TransitionGate.REPAIR:
                    self.repair_transition(canonical_request_id)
                    continue
                if gate == TransitionGate.RETURN:
                    return self.get(canonical_request_id) or existing
                if gate == TransitionGate.HARD_BLOCK:
                    entry = await self._reconcile_or_hard_block_async(
                        canonical_request_id, tool, args, kwargs, existing, binding
                    )
                    if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                        if getattr(_reconcile_cas_lost, "val", False):
                            _reconcile_cas_lost.val = False
                            await self._poll_side_effecting_async(
                                canonical_request_id,
                                tool=tool,
                                interval=interval,
                                poll_deadline=poll_deadline,
                            )
                            continue
                    return entry
                if gate == TransitionGate.POLL:
                    await self._poll_side_effecting_async(
                        canonical_request_id,
                        tool=tool,
                        interval=interval,
                        poll_deadline=poll_deadline,
                    )
                    continue
                if gate == TransitionGate.ALLOW:
                    settled = await self._prefer_settle_before_unknown_allow_async(
                        canonical_request_id, tool, args, kwargs, existing, binding
                    )
                    if settled is not None:
                        return settled
                    if existing.resolved_terminal_outcome() == TerminalOutcome.UNKNOWN:
                        reset = self._reset_unknown_for_same_key_retry(
                            canonical_request_id,
                            tool,
                            args,
                            kwargs,
                            existing,
                            binding,
                            lease_ttl=ttl,
                        )
                        if reset is not None:
                            return reset
                        if self._blind_never_retries(tool, binding, existing):
                            return self._raise_hard_block(
                                canonical_request_id, tool, existing, binding=binding
                            )
                        continue
                    if self._blind_never_retries(tool, binding, existing):
                        return self._raise_hard_block(
                            canonical_request_id, tool, existing, binding=binding
                        )
                    if self._reclaim_requires_death_signal and not has_worker_death_evidence(
                        existing,
                        now=time.time(),
                        presumed_dead_after=self._presumed_dead_after,
                    ):
                        await self._poll_side_effecting_async(
                            canonical_request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue

            _old_pkey_attempt = (
                existing.provider_key_first_attempt_at
                if existing is not None and existing.provider_idempotency_key is not None
                else None
            )
            entry = self._new_inflight_entry(
                canonical_request_id,
                tool,
                args,
                claim_kwargs,
                binding=binding,
                _provider_key_first_attempt_at=_old_pkey_attempt,
                _provider_idempotency_key=(
                    explicit_provider_key
                    if explicit_provider_key is not None
                    else (
                        existing.provider_idempotency_key
                        if existing is not None
                        else None
                    )
                ),
                _effect_id=effect_id,
            )
            outcome, existing = self._try_claim_inflight(entry, lease_ttl=ttl)
            if outcome == "completed" and existing is not None:
                return existing
            if outcome == "in_flight" and existing is not None:
                canonical_request_id = existing.request_id
                incoming_key = self._effective_incoming_provider_key(
                    binding=binding,
                    kwargs=kwargs,
                    effect_id=effect_id,
                    existing=existing,
                )
                gate = resolve_side_effect_gate(
                    existing,
                    binding,
                    incoming_provider_idempotency_key=incoming_key,
                )
                if gate == TransitionGate.REPAIR:
                    self.repair_transition(canonical_request_id)
                    continue
                if gate == TransitionGate.RETURN:
                    return existing
                if gate == TransitionGate.HARD_BLOCK:
                    entry = await self._reconcile_or_hard_block_async(
                        canonical_request_id, tool, args, kwargs, existing, binding
                    )
                    if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                        if getattr(_reconcile_cas_lost, "val", False):
                            _reconcile_cas_lost.val = False
                            await self._poll_side_effecting_async(
                                canonical_request_id,
                                tool=tool,
                                interval=interval,
                                poll_deadline=poll_deadline,
                            )
                            continue
                    return entry
                if gate == TransitionGate.ALLOW and self._blind_never_retries(
                    tool, binding, existing
                ):
                    await self._poll_side_effecting_async(
                        canonical_request_id,
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
                            canonical_request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue
                await self._poll_side_effecting_async(
                    canonical_request_id,
                    tool=tool,
                    interval=interval,
                    poll_deadline=poll_deadline,
                )
                continue
            if outcome == "claimed":
                claimed = self.get(canonical_request_id)
                return claimed if claimed is not None else entry
            if existing is not None:
                canonical_request_id = existing.request_id
                entry = await self._reconcile_or_hard_block_async(
                    canonical_request_id, tool, args, kwargs, existing, binding
                )
                if entry.resolved_terminal_outcome() == TerminalOutcome.IN_FLIGHT:
                    if getattr(_reconcile_cas_lost, "val", False):
                        _reconcile_cas_lost.val = False
                        await self._poll_side_effecting_async(
                            canonical_request_id,
                            tool=tool,
                            interval=interval,
                            poll_deadline=poll_deadline,
                        )
                        continue
                    return entry
                return entry
            raise LedgerError(
                f"Unexpected claim outcome {outcome!r} for side-effecting tool {tool!r} "
                f"(request_id={canonical_request_id!r})"
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
                    try:
                        self.mark_unknown(
                            request_id,
                            error="timed out polling in-flight side-effecting transition",
                            _expected_from=_IN_FLIGHT_OUTCOMES,
                            _expected_owner=current.owner,
                            _expected_fence=current.fence,
                        )
                    except LedgerOutcomeAlreadySetError:
                        return
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
        expected_fence: int | None = None,
        _expected_from: frozenset[str] | None = None,
        _expected_owner: str | None = None,
        _expected_fence: int | None = None,
    ) -> LedgerEntry:
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot complete unknown request {request_id!r}")
        if expected_fence is not None and _expected_fence is not None:
            if expected_fence != _expected_fence:
                raise LedgerError("conflicting expected fence values")
        fence = expected_fence if expected_fence is not None else _expected_fence
        if fence is None:
            raise LedgerError(f"Completing request {request_id!r} requires the claim fence")
        if existing.effect_protocol_required and not _has_allowed_attempting_decision(existing):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot complete request {request_id!r}: no durable ATTEMPTING decision"
            )
        entry = replace(
            existing,
            status=legacy_status_from_terminal(TerminalOutcome.COMPLETED),
            terminal_outcome=TerminalOutcome.COMPLETED.value,
            result=_evidence_value(result),
            finished_at=time.time(),
            lease_until=None,
            side_effect_boundary=SideEffectBoundary.CROSSED.value,
            effect_phase=EffectState.COMMITTED.value,
        )
        if not self._try_transition(
            entry,
            expected_from=_expected_from,
            expected_owner=_expected_owner,
            expected_fence=fence,
            expected_effect_state=(
                EffectState.ATTEMPTING.value
                if existing.effect_protocol_required
                else None
            ),
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
        expected_fence: int | None = None,
        _expected_from: frozenset[str] | None = None,
        _expected_owner: str | None = None,
        _expected_fence: int | None = None,
    ) -> LedgerEntry:
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot fail unknown request {request_id!r}")
        if expected_fence is not None and _expected_fence is not None:
            if expected_fence != _expected_fence:
                raise LedgerError("conflicting expected fence values")
        fence = expected_fence if expected_fence is not None else _expected_fence
        if fence is None:
            raise LedgerError(f"Failing request {request_id!r} requires the claim fence")
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
            error=_evidence_error(error),
            finished_at=time.time(),
            lease_until=None,
            side_effect_boundary=boundary,
            effect_phase=(
                existing.effect_phase
                if failed_after_effect
                and existing.effect_protocol_required
                and existing.effect_phase == EffectState.ATTEMPTING.value
                and existing.decision is not None
                else EffectState.ABORTED.value
            ),
        )
        if not self._try_transition(
            entry,
            expected_from=_expected_from,
            expected_owner=_expected_owner,
            expected_fence=fence,
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

    def attach_receipt_ref(
        self,
        request_id: str,
        receipt_ref: str,
        *,
        expected_owner: str | None = None,
        expected_fence: int | None = None,
    ) -> LedgerEntry:
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot attach receipt to unknown request {request_id!r}")
        if expected_fence is None:
            raise LedgerError(f"Attaching a receipt to {request_id!r} requires the claim fence")
        entry = replace(existing, receipt_ref=receipt_ref)
        if not self._try_transition(
            entry,
            expected_from=frozenset({existing.terminal_outcome}),
            expected_owner=expected_owner,
            expected_fence=expected_fence,
        ):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot attach receipt to {request_id!r}: transition superseded"
            )
        return entry

    def attach_external_operation_ref(
        self,
        request_id: str,
        ref: str,
        *,
        expected_owner: str | None = None,
        expected_fence: int | None = None,
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
        if expected_fence is None:
            raise LedgerError(
                f"Attaching an external operation to {request_id!r} requires the claim fence"
            )
        if existing.effect_protocol_required and not _has_allowed_attempting_decision(existing):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot attach external operation ref to {request_id!r}: "
                "no durable ATTEMPTING decision"
            )
        entry = replace(existing, external_operation_ref=ref)
        if not self._try_transition(
            entry,
            expected_from=frozenset({existing.terminal_outcome}),
            expected_owner=expected_owner,
            expected_fence=expected_fence,
            expected_effect_state=(
                EffectState.ATTEMPTING.value if existing.effect_protocol_required else None
            ),
        ):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot attach external operation ref to {request_id!r}: transition superseded"
            )
        return entry

    def attach_provider_idempotency_key(
        self,
        request_id: str,
        provider_key: str,
        *,
        expected_owner: str | None = None,
        expected_fence: int | None = None,
    ) -> LedgerEntry:
        """Persist provider idempotency key on the claimed transition row.

        Used by wrapper-path auto-propagation (effect_id -> provider key):
        after ATTEMPTING decision CAS succeeds, before tool body starts.
        """
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(
                f"Cannot attach provider idempotency key to unknown request {request_id!r}"
            )
        if expected_fence is None:
            raise LedgerError(
                f"Attaching provider idempotency key to {request_id!r} requires the claim fence"
            )
        key = str(provider_key)
        stored_provider_key = existing.provider_idempotency_key
        if stored_provider_key is not None and stored_provider_key != key:
            raise LedgerOutcomeAlreadySetError(
                f"Cannot attach provider idempotency key to {request_id!r}: key mismatch "
                f"({existing.provider_idempotency_key!r} != {key!r})"
            )
        first_attempt = existing.provider_key_first_attempt_at
        if first_attempt is None:
            first_attempt = time.time()
        entry = replace(
            existing,
            provider_idempotency_key=key,
            provider_key_first_attempt_at=first_attempt,
        )
        if not self._try_transition(
            entry,
            expected_from=frozenset({existing.terminal_outcome}),
            expected_owner=expected_owner,
            expected_fence=expected_fence,
            expected_effect_state=(
                EffectState.ATTEMPTING.value if existing.effect_protocol_required else None
            ),
        ):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot attach provider idempotency key to {request_id!r}: transition superseded"
            )
        return entry

    def renew_lease(
        self,
        request_id: str,
        *,
        lease_ttl: float | None = None,
        now: float | None = None,
        expected_fence: int | None = None,
        _expected_owner: str | None = None,
        _expected_fence: int | None = None,
    ) -> LedgerEntry:
        """Extend ``lease_until`` for an in-flight transition.

        Owner-side heartbeat for long work: keeps peers on ``POLL`` instead of
        opening reclaim. This is the renew half of the ``REPAIR`` taxonomy
        (heal incomplete durable fields via :meth:`repair_transition`; extend a
        still-held lease here). Only applies while the stored terminal outcome
        is still ``IN_FLIGHT`` (before lease expiry is applied). Renewing after
        the lease has already expired raises :class:`LedgerError` — reclaim /
        reconcile must run instead of silently re-asserting ownership.

        Uses CAS (``try_transition``) with owner + lease-held preconditions so
        a concurrent complete / reclaim / expiry cannot be clobbered by a
        stale renew (TOCTOU).

        Backs :func:`renew_lease`.
        """
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot renew lease for unknown request {request_id!r}")
        if expected_fence is not None and _expected_fence is not None:
            if expected_fence != _expected_fence:
                raise LedgerError("conflicting expected fence values")
        fence = expected_fence if expected_fence is not None else _expected_fence
        if fence is None:
            raise LedgerError(f"Renewing request {request_id!r} requires the claim fence")
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
        if not self._try_transition(
            entry,
            expected_from=_IN_FLIGHT_OUTCOMES,
            expected_owner=(existing.owner if _expected_owner is None else _expected_owner),
            require_lease_held_at=now,
            expected_fence=fence,
        ):
            current = self._get_entry(request_id)
            if current is None:
                raise LedgerError(f"Cannot renew lease for unknown request {request_id!r}")
            current_outcome = (
                current.terminal_outcome
                if isinstance(current.terminal_outcome, TerminalOutcome)
                else TerminalOutcome(str(current.terminal_outcome))
            )
            if current_outcome != TerminalOutcome.IN_FLIGHT:
                raise LedgerError(
                    f"Cannot renew lease for request {request_id!r}: "
                    f"terminal_outcome is {current_outcome.value}, not IN_FLIGHT"
                )
            if current.owner != existing.owner:
                raise LedgerError(
                    f"Cannot renew lease for request {request_id!r}: "
                    "owner changed (reclaimed by peer)"
                )
            if resolve_lease_validity(current.lease_until, now=now) == (LeaseValidity.EXPIRED):
                raise LedgerError(
                    f"Cannot renew lease for request {request_id!r}: "
                    "lease already expired — reclaim or reconcile instead"
                )
            raise LedgerError(
                f"Cannot renew lease for request {request_id!r}: "
                "concurrent transition rejected renew"
            )
        return entry

    def advance_boundary(
        self,
        request_id: str,
        boundary: SideEffectBoundary,
        *,
        expected_owner: str | None = None,
        expected_fence: int | None = None,
    ) -> LedgerEntry:
        """Move an entry's side-effect boundary forward (monotonic).

        Only advances toward ``CROSSED`` and never regresses, so concurrent or
        out-of-order markers cannot weaken a stronger recorded boundary. Backs
        the :func:`side_effect` marker used by side-effecting tools.
        """
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot advance boundary for unknown request {request_id!r}")
        if expected_fence is None:
            raise LedgerError(f"Advancing request {request_id!r} requires the claim fence")
        if existing.effect_protocol_required and not _has_allowed_attempting_decision(existing):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot advance boundary for {request_id!r}: "
                "no durable ATTEMPTING decision"
            )
        current = SideEffectBoundary(existing.side_effect_boundary)
        entry = (
            existing
            if _BOUNDARY_RANK[boundary] <= _BOUNDARY_RANK[current]
            else replace(existing, side_effect_boundary=boundary.value)
        )
        if not self._try_transition(
            entry,
            expected_from=frozenset({existing.terminal_outcome}),
            expected_owner=expected_owner,
            expected_fence=expected_fence,
            expected_effect_state=(
                EffectState.ATTEMPTING.value if existing.effect_protocol_required else None
            ),
        ):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot advance boundary for {request_id!r}: transition superseded"
            )
        return entry

    def record_decision(
        self,
        request_id: str,
        decision: dict[str, Any],
        *,
        expected_owner: str | None = None,
        expected_fence: int | None = None,
    ) -> LedgerEntry:
        """Stamp the single-decision-point result onto the entry atomically.

        The write is the ``INTENDED -> ATTEMPTING`` transition: it goes through
        the same fenced compare-and-swap as every other in-flight mutation, so a
        superseded worker (stale fence) cannot record a decision — and therefore
        cannot smuggle in an effect the current-fence decision would deny. The
        entry stays ``IN_FLIGHT``; only the durable ``decision`` field changes.
        """
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot record decision for unknown request {request_id!r}")
        if expected_fence is None:
            raise LedgerError(f"Recording a decision for {request_id!r} requires the claim fence")
        from mycelium.decision import Decision

        try:
            parsed = Decision.from_dict(decision)
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerError(f"Invalid decision for request {request_id!r}: {exc}") from exc
        from mycelium.secret_protection import sanitize_for_decision_evidence

        parsed = Decision.from_dict(sanitize_for_decision_evidence(parsed.to_dict()))
        entry = replace(
            existing,
            decision=parsed.to_dict(),
            effect_phase=(
                EffectState.ATTEMPTING.value if parsed.allowed else EffectState.ABORTED.value
            ),
        )
        if not self._try_transition(
            entry,
            expected_from=_IN_FLIGHT_OUTCOMES,
            expected_owner=expected_owner,
            expected_fence=expected_fence,
            expected_effect_state=EffectState.INTENDED.value,
        ):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot record decision for {request_id!r}: "
                "transition superseded (stale fence/owner or already resolved)"
            )
        return entry

    # --- request id derivation ---

    def derive_request_id(
        self,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        *,
        transition_binding: ToolTransitionBinding | None = None,
        identity_kwargs: dict[str, Any] | None = None,
    ) -> str:
        """Determine the request id for a tool invocation.

        An explicit ``request_id`` kwarg is the transition identity: retries
        that reuse it map to the same ledger entry. The host must derive it
        from a stable, server-owned business record
        (``f"charge-order:{order_id}"``), not from model output.

        ``request_id_from`` on the binding mints
        ``{tool}:{field}:{value}`` when ``request_id`` is omitted.

        When both are omitted:

        * ``require_explicit`` + a consequential side-effect class raises
          :class:`MissingRequestIdentityError` (no ``tool_call_id`` /
          random fallback).
        * ``derived`` (default) keeps the previous identity:
          transition key, then ``tool_call_id``, Session hash, or UUID.

        ``request_id`` is never part of the argument fingerprint and is not
        forwarded to the wrapped tool.
        """
        lookup = identity_kwargs if identity_kwargs is not None else kwargs
        explicit = parse_explicit_request_id(kwargs) or parse_explicit_request_id(lookup)
        if explicit is not None:
            return explicit

        field = transition_binding.request_id_from if transition_binding is not None else None
        if field:
            return request_id_from_argument(tool, field, lookup)

        if (
            self._request_identity_policy == REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT
            and transition_binding is not None
            and transition_binding.side_effect_class in CONSEQUENTIAL_SIDE_EFFECT_CLASSES
        ):
            raise MissingRequestIdentityError(tool=tool)

        if transition_binding is not None:
            return derive_transition_key_for_call(tool, args, kwargs, transition_binding)

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
        from mycelium.secret_protection import fingerprint_args, get_active_secret_policy

        policy = get_active_secret_policy()
        if policy is not None and policy.enabled:
            digest = fingerprint_args(args, kwargs)
            return digest[:16]
        payload = json.dumps(
            {"args": args, "kwargs": kwargs},
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode()).hexdigest()[:16]




def _mark_ledgered(
    wrapper: Callable[..., Any],
    ledger: ActionLedger,
    transition_binding: ToolTransitionBinding | None,
) -> None:
    wrapper._mycelium_ledger = True  # type: ignore[attr-defined]
    wrapper._mycelium_ledger_instance = ledger  # type: ignore[attr-defined]
    wrapper._mycelium_transition_binding = transition_binding  # type: ignore[attr-defined]


def ledger(
    storage: LedgerStorage | None = None,
    audit_emitter: AuditReceiptEmitter | None = None,
    transition_binding: ToolTransitionBinding | None = None,
    *,
    outcome_emitter: OutcomeEmitter | None = None,
    operator_authorizer: OperatorAuthorizer | None = None,
    lease_ttl: float | None = None,
    lease_renew_interval: float | None = None,
    poll_interval: float | None = None,
    poll_timeout: float | None = None,
    reconciler: Reconciler | None = None,
    defer_read_only_unknown: bool = False,
    unclassified_policy: str = UNCLASSIFIED_POLICY_WARN,
    on_args_drift: str = ARGS_DRIFT_SOFT,
    reclaim_requires_death_signal: bool = False,
    presumed_dead_after: float | None = None,
    request_identity_policy: str = REQUEST_IDENTITY_POLICY_DERIVED,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Decorator that records async tool invocations in an ActionLedger.

    While the tool body runs, Mycelium auto-extends the execution lease
    (default every ``lease_ttl / 3``). Pass ``lease_renew_interval=0`` to
    disable; use :func:`renew_lease` for an extra manual bump.
    """

    ledger_kwargs: dict[str, float | bool | None | str] = {}
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
        operator_authorizer=operator_authorizer,
        unclassified_policy=unclassified_policy,
        on_args_drift=on_args_drift,
        request_identity_policy=request_identity_policy,
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

        _mark_ledgered(wrapper, action_ledger, transition_binding)
        return wrapper

    return decorator


def ledger_sync(
    storage: LedgerStorage | None = None,
    audit_emitter: AuditReceiptEmitter | None = None,
    transition_binding: ToolTransitionBinding | None = None,
    *,
    outcome_emitter: OutcomeEmitter | None = None,
    operator_authorizer: OperatorAuthorizer | None = None,
    lease_ttl: float | None = None,
    lease_renew_interval: float | None = None,
    poll_interval: float | None = None,
    poll_timeout: float | None = None,
    reconciler: Reconciler | None = None,
    defer_read_only_unknown: bool = False,
    unclassified_policy: str = UNCLASSIFIED_POLICY_WARN,
    on_args_drift: str = ARGS_DRIFT_SOFT,
    reclaim_requires_death_signal: bool = False,
    presumed_dead_after: float | None = None,
    request_identity_policy: str = REQUEST_IDENTITY_POLICY_DERIVED,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator that records sync tool invocations in an ActionLedger.

    While the tool body runs, Mycelium auto-extends the execution lease
    (default every ``lease_ttl / 3``). Pass ``lease_renew_interval=0`` to
    disable; use :func:`renew_lease` for an extra manual bump.
    """

    ledger_kwargs: dict[str, float | bool | None | str] = {}
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
        operator_authorizer=operator_authorizer,
        unclassified_policy=unclassified_policy,
        on_args_drift=on_args_drift,
        request_identity_policy=request_identity_policy,
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

        _mark_ledgered(wrapper, action_ledger, transition_binding)
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
    "ARGS_DRIFT_OFF",
    "ARGS_DRIFT_SOFT",
    "ARGS_DRIFT_HARD",
    "ARGS_DRIFT_POLICIES",
    "UNCLASSIFIED_POLICY_WARN",
    "UNCLASSIFIED_POLICY_STRICT",
    "REQUEST_IDENTITY_POLICY_DERIVED",
    "REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT",
    "REQUEST_IDENTITY_POLICIES",
    "MissingRequestIdentityError",
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
    "mark_maybe_crossed_async",
    "record_external_operation",
    "renew_lease",
    "side_effect",
    "side_effect_async",
]
