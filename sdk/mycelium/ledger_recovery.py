"""Recovery, reconciliation, retention, and operator workflows for the ledger."""

from __future__ import annotations

import asyncio
import time
import warnings
from dataclasses import replace
from typing import Any

from mycelium.ledger_context import (
    _outcome_reexec_authorized,
    _reconcile_cas_lost,
)
from mycelium.ledger_identity import _evidence_value
from mycelium.ledger_model import (
    _IN_FLIGHT_OUTCOMES,
    _RECONCILE_NOT_EXECUTED_OUTCOMES,
    _RESOLUTION_ACCEPTED_STORED_OUTCOMES,
    DEFAULT_LEASE_TTL,
    OPERATOR_RESOLUTION_COMPLETED,
    OPERATOR_RESOLUTION_NOT_EXECUTED,
    LedgerAlreadyResolvedError,
    LedgerEntry,
    LedgerError,
    LedgerHardBlockError,
    LedgerOutcomeAlreadySetError,
    LedgerPollTimeoutError,
    LedgerReleaseRefusedError,
    LedgerWorkerAliveError,
    _has_allowed_attempting_decision,
)
from mycelium.ledger_storage import LedgerStorage, _storage_errors
from mycelium.reconcile import ReconcileResult, ReconcileStatus
from mycelium.storage.transition_query import TransitionPage, entry_sort_key
from mycelium.transition import (
    EffectState,
    SideEffectBoundary,
    TerminalOutcome,
    ToolCapability,
    ToolTransitionBinding,
    has_worker_death_evidence,
    legacy_status_from_terminal,
)
from mycelium.transition_resolution import (
    hard_block_message,
    repair_transition_fields,
    transition_needs_repair,
)

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


class LedgerRecoveryMixin:
    def list_transitions(
        self,
        *,
        stuck: bool = False,
        tool: str | None = None,
        outcome: TerminalOutcome | None = None,
        parent_request_id: str | None = None,
        in_flight_stuck_after: float = DEFAULT_LEASE_TTL,
    ) -> list[LedgerEntry]:
        """List ledger entries for operator triage (read-only).

        ``stuck=True`` keeps transitions that need a human: resolved terminal
        outcome ``BLOCKED`` / ``UNKNOWN`` / ``FAILED_AFTER_EFFECT`` /
        ``EXPIRED``, plus ``IN_FLIGHT`` entries older than
        ``in_flight_stuck_after`` seconds (an in-flight entry whose lease can
        never expire — e.g. unbounded — would otherwise be invisible forever).
        ``tool`` filters by tool name; ``outcome`` filters by the resolved
        terminal outcome (lease validity applied). ``parent_request_id`` keeps
        children of a handoff parent (thin causation audit). Sorted oldest first.
        """
        now = time.time()
        entries: list[LedgerEntry] = []
        for entry in self._list_all_entries():
            if tool is not None and entry.tool != tool:
                continue
            if parent_request_id is not None and entry.parent_request_id != parent_request_id:
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

    def list_transitions_page(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        stuck: bool = False,
        tool: str | None = None,
        outcome: TerminalOutcome | None = None,
        parent_request_id: str | None = None,
        started_after: float | None = None,
        started_before: float | None = None,
        finished_before: float | None = None,
        in_flight_stuck_after: float = DEFAULT_LEASE_TTL,
    ) -> TransitionPage[LedgerEntry]:
        """Return one bounded, cursor-addressable transition page.

        Postgres and Redis apply status/time filters before transferring rows.
        Lease-derived ``stuck`` classification remains local because it depends
        on the caller's current clock.
        """
        with _storage_errors("list_page"):
            list_page = getattr(self._storage, "list_page", None)
            if list_page is None:
                list_page = LedgerStorage.list_page.__get__(self._storage, type(self._storage))
            page = list_page(
                limit=limit,
                cursor=cursor,
                tool=tool,
                outcome=(
                    outcome.value
                    if outcome is not None and outcome != TerminalOutcome.EXPIRED
                    else None
                ),
                parent_request_id=parent_request_id,
                started_after=started_after,
                started_before=started_before,
                finished_before=finished_before,
            )
        now = time.time()
        entries: list[LedgerEntry] = []
        for entry in page.entries:
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
        return TransitionPage(entries, page.next_cursor)

    def prune_transitions(
        self,
        *,
        before: float | None = None,
        outcomes: frozenset[TerminalOutcome] | None = None,
        dry_run: bool = True,
        limit: int = 1000,
    ) -> tuple[list[LedgerEntry], int]:
        """Preview or delete retained terminal transitions.

        By default only unambiguous terminal outcomes are eligible. Ambiguous,
        blocked, expired, and in-flight records require an explicit outcome set.
        """
        if before is None:
            retention_seconds = getattr(self._storage, "retention_seconds", None)
            if retention_seconds is None:
                raise ValueError("pruning requires --before or a storage retention policy")
            before = time.time() - float(retention_seconds)
        selected_outcomes = outcomes or frozenset(
            {TerminalOutcome.COMPLETED, TerminalOutcome.FAILED_BEFORE_EFFECT}
        )
        candidates: list[LedgerEntry] = []
        for selected in sorted(selected_outcomes, key=lambda item: item.value):
            next_cursor: str | None = None
            while True:
                page = self.list_transitions_page(
                    limit=limit,
                    cursor=next_cursor,
                    outcome=selected,
                    finished_before=before,
                )
                candidates.extend(page.entries)
                next_cursor = page.next_cursor
                if next_cursor is None:
                    break
        candidates.sort(key=entry_sort_key)
        if dry_run or not candidates:
            return candidates, 0
        with _storage_errors("delete_entries"):
            deleted = self._storage.delete_entries([entry.request_id for entry in candidates])
        return candidates, deleted

    def delete_transitions(self, request_ids: list[str]) -> int:
        """Delete an already reviewed/archive-written set of transition ids."""
        with _storage_errors("delete_entries"):
            return self._storage.delete_entries(request_ids)

    def wait_for_transition(
        self,
        request_id: str,
        *,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> LedgerEntry:
        """Block until ``request_id`` leaves ``IN_FLIGHT`` (sync peer wait).

        DX helper for custom claim/redispatch loops. Decorator claim paths
        already poll; use this when coordinating outside ``@ledger`` /
        ``@ledger_sync``. Does not reclaim, reconcile, or mark ``UNKNOWN`` on
        timeout — callers own the next gate. Raises
        :class:`LedgerError` if the entry is missing;
        :class:`LedgerPollTimeoutError` if still in-flight at deadline.
        """
        interval = self._poll_interval if poll_interval is None else poll_interval
        timeout = self._poll_timeout if poll_timeout is None else poll_timeout
        poll_deadline = time.time() + timeout if timeout is not None else None
        while True:
            current = self.get(request_id)
            if current is None:
                raise LedgerError(f"Cannot wait for unknown request {request_id!r}")
            outcome = current.resolved_terminal_outcome()
            if outcome != TerminalOutcome.IN_FLIGHT:
                return current
            if poll_deadline is not None and time.time() >= poll_deadline:
                raise LedgerPollTimeoutError(f"Timed out waiting for request {request_id!r}")
            time.sleep(interval)

    async def wait_for_transition_async(
        self,
        request_id: str,
        *,
        poll_interval: float | None = None,
        poll_timeout: float | None = None,
    ) -> LedgerEntry:
        """Async peer wait for LangGraph-style redispatches (``asyncio.sleep``).

        Same semantics as :meth:`wait_for_transition`, but does not block the
        event loop. Prefer this from async tools / custom nodes when a peer
        already holds the lease and you want the terminal entry without
        re-claiming.
        """
        interval = self._poll_interval if poll_interval is None else poll_interval
        timeout = self._poll_timeout if poll_timeout is None else poll_timeout
        poll_deadline = time.time() + timeout if timeout is not None else None
        while True:
            current = self.get(request_id)
            if current is None:
                raise LedgerError(f"Cannot wait for unknown request {request_id!r}")
            outcome = current.resolved_terminal_outcome()
            if outcome != TerminalOutcome.IN_FLIGHT:
                return current
            if poll_deadline is not None and time.time() >= poll_deadline:
                raise LedgerPollTimeoutError(f"Timed out waiting for request {request_id!r}")
            await asyncio.sleep(interval)

    def release(
        self,
        request_id: str,
        *,
        verified: str,
        result: Any = None,
        by: str,
        reason: str,
        credential: str | None = None,
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
            raise LedgerReleaseRefusedError(f"Cannot release unknown request {request_id!r}")
        if self._operator_authorizer is not None:
            from mycelium.operator_auth import OperatorReleaseRequest

            authorization = OperatorReleaseRequest(
                operator_id=by,
                request_id=request_id,
                tool=existing.tool,
                verified=verified,
                effect_id=existing.effect_id,
                tenant=existing.tenant_id,
                policy_version=existing.policy_version,
            )
            try:
                allowed = self._operator_authorizer.authorize_release(
                    authorization,
                    credential=credential,
                )
            except Exception as exc:
                raise LedgerReleaseRefusedError("operator authorization failed closed") from exc
            if not allowed:
                raise LedgerReleaseRefusedError(
                    f"operator {by!r} is not authorized to release request {request_id!r}"
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
            if self._reclaim_requires_death_signal and not has_worker_death_evidence(
                existing,
                now=now,
                presumed_dead_after=self._presumed_dead_after,
            ):
                grace = _grace_remaining(
                    existing,
                    now=now,
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
            if existing.effect_protocol_required and not _has_allowed_attempting_decision(existing):
                raise LedgerReleaseRefusedError(
                    f"Cannot release request {request_id!r} as completed: "
                    "no allowed durable ATTEMPTING decision"
                )
            entry = replace(
                existing,
                status=legacy_status_from_terminal(TerminalOutcome.COMPLETED),
                terminal_outcome=TerminalOutcome.COMPLETED.value,
                result=_evidence_value(result),
                finished_at=now,
                lease_until=None,
                side_effect_boundary=SideEffectBoundary.CROSSED.value,
                effect_phase=EffectState.COMMITTED.value,
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
        if not self._try_transition(
            entry,
            expected_from=_RESOLUTION_ACCEPTED_STORED_OUTCOMES,
            expected_owner=existing.owner,
            expected_fence=existing.fence,
        ):
            raise LedgerAlreadyResolvedError(
                f"Cannot release request {request_id!r}: transition superseded"
            )
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
            entry = self.attach_receipt_ref(
                request_id,
                receipt.receipt_id,
                expected_owner=entry.owner,
                expected_fence=entry.fence,
            )
        return entry

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
                        _expected_fence=current.fence,
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
        observed_entry: LedgerEntry,
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
            if observed_entry.effect_protocol_required and not (
                _has_allowed_attempting_decision(observed_entry)
            ):
                return None
            try:
                return self.complete(
                    request_id,
                    result.result,
                    _expected_from=_RESOLUTION_ACCEPTED_STORED_OUTCOMES,
                    _expected_owner=observed_entry.owner,
                    _expected_fence=observed_entry.fence,
                )
            except LedgerOutcomeAlreadySetError:
                if _cas_race_returns_none:
                    return None
                _reconcile_cas_lost.val = True
                return self.get(request_id)
        if result.status == ReconcileStatus.NOT_EXECUTED:
            if _preserved_pkey_first_attempt is None:
                if observed_entry.provider_idempotency_key is not None:
                    _preserved_pkey_first_attempt = observed_entry.provider_key_first_attempt_at
            fresh = self._new_inflight_entry(
                request_id,
                tool,
                args,
                kwargs,
                binding=binding,
                _provider_key_first_attempt_at=_preserved_pkey_first_attempt,
            )
            now = time.time()
            fresh = replace(
                fresh,
                fence=observed_entry.fence + 1,
                lease_until=(now + self._lease_ttl if self._lease_ttl > 0 else None),
                last_heartbeat_at=now,
            )
            # EXPIRED entries have stored terminal ``IN_FLIGHT`` (lease is
            # resolved at read time).  Advance past ``IN_FLIGHT`` first so the
            # CAS below cannot race on ``IN_FLIGHT → IN_FLIGHT``.
            expected_from = _RECONCILE_NOT_EXECUTED_OUTCOMES
            if observed_entry.resolved_terminal_outcome(now=now) in (TerminalOutcome.EXPIRED,):
                try:
                    self.mark_blocked(
                        request_id,
                        error="reconciling expired entry as NOT_EXECUTED",
                        _expected_from=_IN_FLIGHT_OUTCOMES,
                        _expected_owner=observed_entry.owner,
                        _expected_fence=observed_entry.fence,
                    )
                except LedgerOutcomeAlreadySetError:
                    pass
                expected_from = frozenset({TerminalOutcome.BLOCKED.value})
            if not self._try_transition(
                fresh,
                expected_from=expected_from,
                expected_owner=observed_entry.owner,
                expected_fence=observed_entry.fence,
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

    def _capability_for(self, binding: ToolTransitionBinding) -> ToolCapability:
        """Effective capability for this ledger — reconciler presence drives QUERYABLE.

        A bound :class:`~mycelium.reconcile.Reconciler` is the concrete
        "queryable" mechanism, so it can loosen the binding's conservative floor
        (e.g. ``NON_IDEMPOTENT_MUTATE`` BLIND → QUERYABLE). An explicit ``BLIND``
        declaration always wins and is never loosened.
        """
        return binding.effective_capability(has_reconciler=self._reconciler is not None)

    def _entry_is_ambiguous(self, entry: LedgerEntry) -> bool:
        """Whether an effect's outcome is unknown (may or may not have happened).

        A ``FAILED_BEFORE_EFFECT`` or ``EXPIRED`` entry whose boundary is still
        ``not_crossed`` is not ambiguous — the effect provably never crossed the
        boundary, so it stays safe to retry (or death-signal reclaim) regardless
        of probeability. Ambiguity is ``UNKNOWN`` / ``FAILED_AFTER_EFFECT`` (the
        outcome itself is unknown or the effect definitely fired), or *any*
        ``maybe_crossed`` / ``crossed`` boundary. Only ambiguous entries are
        subject to BLIND parking — that is exactly the "did the blind effect
        happen?" case.
        """
        outcome = entry.resolved_terminal_outcome()
        if outcome in (
            TerminalOutcome.UNKNOWN,
            TerminalOutcome.FAILED_AFTER_EFFECT,
        ):
            return True
        boundary = SideEffectBoundary(entry.side_effect_boundary)
        return boundary in (
            SideEffectBoundary.MAYBE_CROSSED,
            SideEffectBoundary.CROSSED,
        )

    def _blind_never_retries(
        self,
        tool: str,
        binding: ToolTransitionBinding,
        existing: LedgerEntry,
    ) -> bool:
        """Whether this tool must park (never auto-retry) an ambiguous entry.

        BLIND: no way to probe the outcome — never auto-redispatch an entry
        whose effect may have crossed the boundary. QUERYABLE without a
        reconciler present fails closed to the same parking behaviour (with a
        warning) rather than silently auto-retrying a second effect. An
        unambiguous ``FAILED_BEFORE_EFFECT`` / ``not_crossed`` entry is never
        parked here — it provably did not happen.
        """
        if not self._entry_is_ambiguous(existing):
            return False
        capability = self._capability_for(binding)
        has_provider_key = binding.provider_idempotency_key_param is not None
        # A tool that intended to be QUERYABLE but has no probe mechanism (no
        # reconciler bound, no provider idempotency key) fails closed to BLIND
        # parking — with a warning so the misconfiguration is visible.
        intended_queryable = (
            binding.explicit_capability == ToolCapability.QUERYABLE
            or binding.capability == ToolCapability.QUERYABLE
        )
        if (
            capability == ToolCapability.BLIND
            and intended_queryable
            and not has_provider_key
            and self._reconciler is None
        ):
            warnings.warn(
                f"tool {tool!r} declares capability=queryable but no Reconciler "
                "is bound and no provider idempotency key is configured; "
                "failing closed to blind behaviour — the ambiguous entry parks "
                "for operator reconciliation instead of auto-retrying.",
                stacklevel=2,
            )
            return True
        if capability == ToolCapability.BLIND:
            return True
        # QUERYABLE with a provider idempotency key needs no reconciler: the
        # same-key retry gate already validated the dedupe window.
        return False

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
            request_id,
            tool,
            args,
            kwargs,
            binding,
            result,
            existing,
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
            request_id,
            tool,
            args,
            kwargs,
            binding,
            result,
            existing,
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
            existing,
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
        if not self._try_transition(
            stamped,
            expected_from=frozenset({fresh.terminal_outcome}),
            expected_owner=fresh.owner,
            expected_fence=fresh.fence,
        ):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot consume release for {request_id!r}: transition superseded"
            )
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
        resolved = self._attempt_reconcile(request_id, tool, args, kwargs, existing, binding)
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

    def _prefer_settle_before_unknown_allow(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        existing: LedgerEntry,
        binding: ToolTransitionBinding,
    ) -> LedgerEntry | None:
        """Prefer operator release / Reconciler before same-key UNKNOWN re-exec.

        Returns a settled entry when resolution succeeded, else ``None`` so the
        claim loop may fall through to the opt-in same-key retry (provider
        dedupe still within ``provider_idempotency_key_ttl``).
        """
        if existing.resolved_terminal_outcome() != TerminalOutcome.UNKNOWN:
            return None
        released = self._consume_operator_resolution(
            request_id, tool, args, kwargs, existing, binding
        )
        if released is not None:
            return released
        return self._attempt_reconcile(request_id, tool, args, kwargs, existing, binding)

    async def _prefer_settle_before_unknown_allow_async(
        self,
        request_id: str,
        tool: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        existing: LedgerEntry,
        binding: ToolTransitionBinding,
    ) -> LedgerEntry | None:
        """Async variant of :meth:`_prefer_settle_before_unknown_allow`."""
        if existing.resolved_terminal_outcome() != TerminalOutcome.UNKNOWN:
            return None
        released = self._consume_operator_resolution(
            request_id, tool, args, kwargs, existing, binding
        )
        if released is not None:
            return released
        return await self._attempt_reconcile_async(
            request_id, tool, args, kwargs, existing, binding
        )

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
                f"Cannot repair request {request_id!r}: still incomplete after safe field updates"
            )
        if not self._try_transition(
            entry,
            expected_from=frozenset({existing.terminal_outcome}),
            expected_owner=existing.owner,
            expected_fence=existing.fence,
        ):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot repair request {request_id!r}: transition superseded"
            )
        return entry

    def mark_blocked(
        self,
        request_id: str,
        *,
        error: str | None = None,
        expected_fence: int | None = None,
        _expected_from: frozenset[str] | None = None,
        _expected_owner: str | None = None,
        _expected_fence: int | None = None,
    ) -> LedgerEntry:
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot block unknown request {request_id!r}")
        if expected_fence is not None and _expected_fence is not None:
            if expected_fence != _expected_fence:
                raise LedgerError("conflicting expected fence values")
        fence = expected_fence if expected_fence is not None else _expected_fence
        if fence is None:
            raise LedgerError(f"Blocking request {request_id!r} requires the claim fence")
        entry = replace(
            existing,
            status=legacy_status_from_terminal(TerminalOutcome.BLOCKED),
            terminal_outcome=TerminalOutcome.BLOCKED.value,
            error=error,
            finished_at=time.time(),
            lease_until=None,
            effect_phase=(
                existing.effect_phase
                if existing.effect_protocol_required
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
        expected_fence: int | None = None,
        _expected_from: frozenset[str] | None = None,
        _expected_owner: str | None = None,
        _expected_fence: int | None = None,
    ) -> LedgerEntry:
        existing = self._get_entry(request_id)
        if existing is None:
            raise LedgerError(f"Cannot mark unknown request {request_id!r}")
        if expected_fence is not None and _expected_fence is not None:
            if expected_fence != _expected_fence:
                raise LedgerError("conflicting expected fence values")
        fence = expected_fence if expected_fence is not None else _expected_fence
        if fence is None:
            raise LedgerError(f"Marking request {request_id!r} unknown requires the claim fence")
        entry = replace(
            existing,
            status=legacy_status_from_terminal(TerminalOutcome.UNKNOWN),
            terminal_outcome=TerminalOutcome.UNKNOWN.value,
            error=error,
            finished_at=time.time(),
            lease_until=None,
            effect_phase=(
                existing.effect_phase
                if existing.effect_protocol_required
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
            raise LedgerReleaseRefusedError("mark_worker_dead requires an operator identity ('by')")
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
                    entry,
                    now=now,
                    presumed_dead_after=self._presumed_dead_after,
                )
                raise LedgerWorkerAliveError(
                    f"Cannot mark worker dead for owner {owner!r}: request "
                    f"{entry.request_id!r} has recent heartbeat "
                    f"({_format_heartbeat_age(entry, now=now)}) — "
                    f"grace window elapses in {grace}"
                )
            stored_reason = f"{reason} (heartbeat overridden)" if override_heartbeat else reason
            dead_entry = replace(
                entry,
                worker_dead_asserted_by=by,
                worker_dead_asserted_at=now,
                resolution_reason=stored_reason,
            )
            if not self._try_transition(
                dead_entry,
                expected_from=frozenset({entry.terminal_outcome}),
                expected_owner=entry.owner,
                expected_fence=entry.fence,
            ):
                continue
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
                existing,
                now=now,
                presumed_dead_after=self._presumed_dead_after,
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
        stored_reason = f"{reason} (heartbeat overridden)" if override_heartbeat else reason
        entry = replace(
            existing,
            worker_dead_asserted_by=by,
            worker_dead_asserted_at=now,
            resolution_reason=stored_reason,
        )
        if not self._try_transition(
            entry,
            expected_from=frozenset({existing.terminal_outcome}),
            expected_owner=existing.owner,
            expected_fence=existing.fence,
        ):
            raise LedgerOutcomeAlreadySetError(
                f"Cannot mark worker dead for {request_id!r}: transition superseded"
            )
        return entry
