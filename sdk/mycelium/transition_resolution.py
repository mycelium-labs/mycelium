"""Resolution rules for side-effecting tool transitions."""

from __future__ import annotations

from typing import Any, Protocol

from mycelium._compat import StrEnum
from mycelium.transition import (
    RetryPermission,
    SideEffectBoundary,
    SideEffectClass,
    Spendability,
    TerminalOutcome,
    ToolTransitionBinding,
    blocks_on_ambiguous_replay,
    legacy_status_from_terminal,
    parse_terminal_outcome,
    terminal_from_legacy_status,
)


class _ExistingTransition(Protocol):
    side_effect_boundary: str
    provider_idempotency_key: str | None

    def resolved_terminal_outcome(self, *, now: float | None = None) -> TerminalOutcome: ...


class TransitionGate(StrEnum):
    """High-level gate decision for a duplicate dispatch."""

    ALLOW = "ALLOW"
    RETURN = "RETURN"
    POLL = "POLL"
    RECLAIM = "RECLAIM"
    REPAIR = "REPAIR"
    SOFT_BLOCK = "SOFT_BLOCK"
    HARD_BLOCK = "HARD_BLOCK"


def _raw_terminal_outcome(existing: _ExistingTransition) -> Any:
    return getattr(existing, "terminal_outcome", None)


def _raw_status(existing: _ExistingTransition) -> str | None:
    status = getattr(existing, "status", None)
    return str(status) if status is not None else None


def _parse_stored_terminal(existing: _ExistingTransition) -> TerminalOutcome | None:
    raw = _raw_terminal_outcome(existing)
    if raw is None or raw == "":
        return None
    try:
        return parse_terminal_outcome(raw)
    except ValueError:
        return None


def _boundary_is_valid(existing: _ExistingTransition) -> bool:
    raw = getattr(existing, "side_effect_boundary", None)
    if raw is None or raw == "":
        return False
    try:
        SideEffectBoundary(str(raw))
    except ValueError:
        return False
    return True


def transition_needs_repair(existing: _ExistingTransition) -> bool:
    """True when the durable record is incomplete but safely healable.

    Detects missing ``idempotency_key``, missing/invalid ``side_effect_boundary``,
    missing/invalid ``terminal_outcome``, or healable status/terminal drift.
    Does **not** treat a held in-flight lease as repair — peers ``POLL``; the
    owner extends via ``renew_lease()``.
    """
    key = getattr(existing, "idempotency_key", None)
    if key is None or key == "":
        return True
    if not _boundary_is_valid(existing):
        return True

    stored = _parse_stored_terminal(existing)
    if stored is None:
        return True

    status = _raw_status(existing)
    if status == "completed" and stored != TerminalOutcome.COMPLETED:
        return True
    if status == "failed" and stored == TerminalOutcome.IN_FLIGHT:
        return True
    return False


def repair_transition_fields(
    existing: _ExistingTransition,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Return field updates that heal an incomplete durable transition record.

    Safe defaults only — never invents a completed result, never renews a peer
    lease, and never weakens a stronger recorded side-effect boundary.
    """
    updates: dict[str, Any] = {}

    key = getattr(existing, "idempotency_key", None)
    request_id = getattr(existing, "request_id", None)
    if (key is None or key == "") and request_id is not None:
        updates["idempotency_key"] = str(request_id)

    if not _boundary_is_valid(existing):
        updates["side_effect_boundary"] = SideEffectBoundary.NOT_CROSSED.value

    stored = _parse_stored_terminal(existing)
    status = _raw_status(existing) or "in-flight"
    lease_until = getattr(existing, "lease_until", None)

    if stored is None:
        healed = terminal_from_legacy_status(status, lease_until=lease_until, now=now)
        updates["terminal_outcome"] = healed.value
        updates["status"] = legacy_status_from_terminal(healed)
    elif status == "completed" and stored != TerminalOutcome.COMPLETED:
        updates["terminal_outcome"] = TerminalOutcome.COMPLETED.value
        updates["status"] = legacy_status_from_terminal(TerminalOutcome.COMPLETED)
    elif status == "failed" and stored == TerminalOutcome.IN_FLIGHT:
        updates["terminal_outcome"] = TerminalOutcome.FAILED_BEFORE_EFFECT.value
        updates["status"] = legacy_status_from_terminal(
            TerminalOutcome.FAILED_BEFORE_EFFECT
        )

    return updates


def resolve_read_only_gate(existing: _ExistingTransition) -> TransitionGate:
    """Decide how to handle an existing transition for a *read-only* tool.

    Read-only tools produce no external effect, so re-running is always safe.
    The gate is therefore lighter than the side-effecting resolver:

    - Incomplete durable record → ``REPAIR`` (heal, then re-resolve)
    - ``COMPLETED`` → ``RETURN`` the stored result
    - ``IN_FLIGHT`` → ``POLL`` while another worker holds a valid lease
    - ``EXPIRED`` / ``FAILED_BEFORE_EFFECT`` / ``FAILED_AFTER_EFFECT`` →
      ``RECLAIM`` (safe to re-execute)
    - ``BLOCKED`` / ``UNKNOWN`` → ``SOFT_BLOCK`` — an ambiguous terminal state
      must not hard-block a reversible read, so the caller defers or retries
      (cost-dependent) instead of parking it for a human.
    """
    if transition_needs_repair(existing):
        return TransitionGate.REPAIR
    outcome = existing.resolved_terminal_outcome()
    if outcome == TerminalOutcome.COMPLETED:
        return TransitionGate.RETURN
    if outcome == TerminalOutcome.IN_FLIGHT:
        return TransitionGate.POLL
    if outcome in (TerminalOutcome.BLOCKED, TerminalOutcome.UNKNOWN):
        return TransitionGate.SOFT_BLOCK
    return TransitionGate.RECLAIM


def _entry_boundary(existing: _ExistingTransition) -> SideEffectBoundary:
    raw = getattr(existing, "side_effect_boundary", SideEffectBoundary.NOT_CROSSED.value)
    try:
        return SideEffectBoundary(str(raw))
    except ValueError:
        return SideEffectBoundary.NOT_CROSSED


def _same_provider_idempotency_key(
    existing: _ExistingTransition,
    incoming_provider_idempotency_key: str | None,
) -> bool:
    """True when the retry provably reuses the stored provider idempotency key."""
    stored = getattr(existing, "provider_idempotency_key", None)
    if stored is None or incoming_provider_idempotency_key is None:
        return False
    return incoming_provider_idempotency_key == stored


def resolve_side_effect_gate(
    existing: _ExistingTransition,
    binding: ToolTransitionBinding,
    *,
    incoming_provider_idempotency_key: str | None = None,
) -> TransitionGate:
    """Decide how to handle an existing transition for a side-effecting tool.

    Incomplete durable context returns ``REPAIR`` first — heal missing key /
    boundary / terminal fields, then re-resolve. Do not execute a second side
    effect while the record is incomplete.

    Lease validity is consulted via ``resolved_terminal_outcome()``:
    a still-``HELD`` lease stays ``IN_FLIGHT`` → ``POLL``; an ``EXPIRED`` lease
    becomes ``EXPIRED`` and then reclaim / hard-block by class and boundary.
    ``lease_until`` is not part of the transition key — renew it while work
    continues so peers keep polling instead of reclaiming.

    ``incoming_provider_idempotency_key`` is the provider idempotency key of the
    redispatched call. It is only consulted when the tool opts into enforcement
    via ``binding.provider_idempotency_key_param``; otherwise the
    ``retry_only_with_same_provider_idempotency_key`` permission stays
    cooperative (backward compatible).
    """
    if transition_needs_repair(existing):
        return TransitionGate.REPAIR

    outcome = existing.resolved_terminal_outcome()
    boundary = _entry_boundary(existing)
    retry = binding.retry_permission
    spendability = binding.spendability

    if outcome == TerminalOutcome.COMPLETED:
        # Same transition key always returns the stored result. multi_use
        # re-spend requires a new intent (new key), not replaying this one.
        return TransitionGate.RETURN
    if outcome == TerminalOutcome.IN_FLIGHT:
        return TransitionGate.POLL

    if outcome in (TerminalOutcome.BLOCKED, TerminalOutcome.UNKNOWN):
        return TransitionGate.HARD_BLOCK

    if outcome == TerminalOutcome.EXPIRED:
        # maybe_crossed / crossed: effect may have happened → HARD_BLOCK.
        # Strict spendability (single_use / non_replayable) also HARD_BLOCKs
        # even on not_crossed: reclaim is only safe when a Reconciler proves
        # NOT_EXECUTED via external_operation_ref (see ActionLedger claim path).
        # multi_use + SAFE_RETRY + not_crossed may ALLOW (idempotent reclaim).
        if boundary in (SideEffectBoundary.MAYBE_CROSSED, SideEffectBoundary.CROSSED):
            return TransitionGate.HARD_BLOCK
        if blocks_on_ambiguous_replay(spendability):
            return TransitionGate.HARD_BLOCK
        if (
            spendability == Spendability.MULTI_USE
            and retry == RetryPermission.SAFE_RETRY
            and boundary == SideEffectBoundary.NOT_CROSSED
        ):
            return TransitionGate.ALLOW
        return TransitionGate.HARD_BLOCK

    if outcome == TerminalOutcome.FAILED_AFTER_EFFECT:
        return TransitionGate.HARD_BLOCK

    if outcome == TerminalOutcome.FAILED_BEFORE_EFFECT:
        if boundary in (SideEffectBoundary.MAYBE_CROSSED, SideEffectBoundary.CROSSED):
            return TransitionGate.HARD_BLOCK
        # Effect never spent — first spend may proceed when retry permission allows,
        # including non_replayable tools that failed before the boundary.
        if retry == RetryPermission.NEVER_RETRY_AUTOMATICALLY:
            return TransitionGate.HARD_BLOCK
        if retry == RetryPermission.MANUAL_RECONCILIATION_REQUIRED:
            return TransitionGate.HARD_BLOCK
        if retry == RetryPermission.RETRY_ONLY_WITH_SAME_PROVIDER_IDEMPOTENCY_KEY:
            if binding.side_effect_class in (
                SideEffectClass.IDEMPOTENT_MUTATE,
                SideEffectClass.KEYED_MUTATE,
            ):
                # Opt-in enforcement: only allow the retry when it provably
                # reuses the same provider idempotency key, so the provider
                # dedupes the second attempt. Without the declaration the
                # permission stays cooperative (allow), as before.
                if binding.provider_idempotency_key_param is not None:
                    if not _same_provider_idempotency_key(
                        existing, incoming_provider_idempotency_key
                    ):
                        return TransitionGate.HARD_BLOCK
                return TransitionGate.ALLOW
            return TransitionGate.HARD_BLOCK
        if retry == RetryPermission.SAFE_RETRY:
            return TransitionGate.ALLOW

    return TransitionGate.HARD_BLOCK


def hard_block_message(
    existing: _ExistingTransition,
    *,
    tool: str,
    request_id: str,
) -> str:
    outcome = existing.resolved_terminal_outcome()
    boundary = _entry_boundary(existing)
    op_ref = getattr(existing, "external_operation_ref", None)
    ref_hint = (
        f" (external_operation_ref={op_ref!r})" if op_ref else ""
    )
    lease_hint = ""
    if outcome == TerminalOutcome.EXPIRED:
        lease_until = getattr(existing, "lease_until", None)
        lease_hint = f", lease_until={lease_until!r}"
    return (
        f"Side-effecting tool {tool!r} request {request_id!r} is "
        f"{outcome.value} with boundary {boundary.value}{lease_hint}{ref_hint}; "
        "manual reconciliation required"
    )


def soft_block_message(
    existing: _ExistingTransition,
    *,
    tool: str,
    request_id: str,
) -> str:
    outcome = existing.resolved_terminal_outcome()
    return (
        f"Read-only tool {tool!r} request {request_id!r} is {outcome.value}; "
        "soft-blocked — safe to defer and retry"
    )


def repair_message(
    existing: _ExistingTransition,
    *,
    tool: str,
    request_id: str,
) -> str:
    return (
        f"Tool {tool!r} request {request_id!r} has incomplete durable transition "
        "context; repair before execute"
    )


__all__ = [
    "TransitionGate",
    "hard_block_message",
    "repair_message",
    "repair_transition_fields",
    "resolve_read_only_gate",
    "resolve_side_effect_gate",
    "soft_block_message",
    "transition_needs_repair",
]
