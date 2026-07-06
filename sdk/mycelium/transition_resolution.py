"""Resolution rules for side-effecting tool transitions."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

from mycelium.transition import (
    RetryPermission,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    is_strict_side_effect,
)


class _ExistingTransition(Protocol):
    side_effect_boundary: str

    def resolved_terminal_outcome(self, *, now: float | None = None) -> TerminalOutcome: ...


class TransitionGate(StrEnum):
    """High-level gate decision for a duplicate dispatch."""

    ALLOW = "ALLOW"
    RETURN = "RETURN"
    POLL = "POLL"
    RECLAIM = "RECLAIM"
    HARD_BLOCK = "HARD_BLOCK"


def _entry_boundary(existing: _ExistingTransition) -> SideEffectBoundary:
    raw = getattr(existing, "side_effect_boundary", SideEffectBoundary.NOT_CROSSED.value)
    return SideEffectBoundary(str(raw))


def _retry_allows_failed_before(retry: RetryPermission) -> bool:
    return retry in (
        RetryPermission.SAFE_RETRY,
        RetryPermission.RETRY_ONLY_WITH_SAME_PROVIDER_IDEMPOTENCY_KEY,
    )


def resolve_side_effect_gate(
    existing: _ExistingTransition,
    binding: ToolTransitionBinding,
) -> TransitionGate:
    """Decide how to handle an existing transition for a side-effecting tool."""
    outcome = existing.resolved_terminal_outcome()
    boundary = _entry_boundary(existing)
    retry = binding.retry_permission

    if outcome == TerminalOutcome.COMPLETED:
        return TransitionGate.RETURN
    if outcome == TerminalOutcome.IN_FLIGHT:
        return TransitionGate.POLL

    if outcome in (TerminalOutcome.BLOCKED, TerminalOutcome.UNKNOWN):
        if boundary in (SideEffectBoundary.MAYBE_CROSSED, SideEffectBoundary.CROSSED):
            return TransitionGate.HARD_BLOCK
        if outcome == TerminalOutcome.UNKNOWN:
            return TransitionGate.HARD_BLOCK
        return TransitionGate.HARD_BLOCK

    if outcome == TerminalOutcome.EXPIRED:
        if boundary in (SideEffectBoundary.MAYBE_CROSSED, SideEffectBoundary.CROSSED):
            return TransitionGate.HARD_BLOCK
        if is_strict_side_effect(binding.side_effect_class):
            return TransitionGate.HARD_BLOCK
        if (
            retry == RetryPermission.SAFE_RETRY
            and boundary == SideEffectBoundary.NOT_CROSSED
            and binding.side_effect_class == SideEffectClass.IDEMPOTENT_WRITE
        ):
            return TransitionGate.ALLOW
        return TransitionGate.HARD_BLOCK

    if outcome == TerminalOutcome.FAILED_AFTER_EFFECT:
        return TransitionGate.HARD_BLOCK

    if outcome == TerminalOutcome.FAILED_BEFORE_EFFECT:
        if boundary in (SideEffectBoundary.MAYBE_CROSSED, SideEffectBoundary.CROSSED):
            return TransitionGate.HARD_BLOCK
        if retry == RetryPermission.NEVER_RETRY_AUTOMATICALLY:
            return TransitionGate.HARD_BLOCK
        if retry == RetryPermission.MANUAL_RECONCILIATION_REQUIRED:
            return TransitionGate.HARD_BLOCK
        if retry == RetryPermission.RETRY_ONLY_WITH_SAME_PROVIDER_IDEMPOTENCY_KEY:
            if binding.side_effect_class in (
                SideEffectClass.IDEMPOTENT_WRITE,
                SideEffectClass.EXTERNAL_API_MUTATION,
            ):
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
    return (
        f"Side-effecting tool {tool!r} request {request_id!r} is "
        f"{outcome.value} with boundary {boundary.value}; "
        "manual reconciliation required"
    )


__all__ = [
    "TransitionGate",
    "hard_block_message",
    "resolve_side_effect_gate",
]
