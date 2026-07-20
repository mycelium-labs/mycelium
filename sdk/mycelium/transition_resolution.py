"""Resolution rules for side-effecting tool transitions."""

from __future__ import annotations

from typing import Protocol

from mycelium._compat import StrEnum
from mycelium.transition import (
    RetryPermission,
    SideEffectBoundary,
    SideEffectClass,
    Spendability,
    TerminalOutcome,
    ToolTransitionBinding,
    blocks_on_ambiguous_replay,
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
    SOFT_BLOCK = "SOFT_BLOCK"
    HARD_BLOCK = "HARD_BLOCK"


def resolve_read_only_gate(existing: _ExistingTransition) -> TransitionGate:
    """Decide how to handle an existing transition for a *read-only* tool.

    Read-only tools produce no external effect, so re-running is always safe.
    The gate is therefore lighter than the side-effecting resolver:

    - ``COMPLETED`` → ``RETURN`` the stored result
    - ``IN_FLIGHT`` → ``POLL`` while another worker holds a valid lease
    - ``EXPIRED`` / ``FAILED_BEFORE_EFFECT`` / ``FAILED_AFTER_EFFECT`` →
      ``RECLAIM`` (safe to re-execute)
    - ``BLOCKED`` / ``UNKNOWN`` → ``SOFT_BLOCK`` — an ambiguous terminal state
      must not hard-block a reversible read, so the caller defers or retries
      (cost-dependent) instead of parking it for a human.
    """
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
    return SideEffectBoundary(str(raw))


def _retry_allows_failed_before(retry: RetryPermission) -> bool:
    return retry in (
        RetryPermission.SAFE_RETRY,
        RetryPermission.RETRY_ONLY_WITH_SAME_PROVIDER_IDEMPOTENCY_KEY,
    )


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

    Lease validity is consulted first via ``resolved_terminal_outcome()``:
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


__all__ = [
    "TransitionGate",
    "hard_block_message",
    "resolve_read_only_gate",
    "resolve_side_effect_gate",
    "soft_block_message",
]
