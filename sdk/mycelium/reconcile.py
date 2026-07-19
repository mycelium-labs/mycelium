"""Reconciliation of ambiguous side-effecting transitions.

When a side-effecting transition ends ambiguous (``UNKNOWN`` /
``FAILED_AFTER_EFFECT`` / ``maybe_crossed``) and an ``external_operation_ref``
was recorded via :func:`mycelium.record_external_operation`, a ``Reconciler``
can query the provider to determine what actually happened and resolve the
transition automatically instead of hard-blocking for a human.

A ``Reconciler`` **must be read-only**: it may look up the provider operation
identified by ``entry.external_operation_ref`` but must never create, mutate, or
retry the external effect. It returns a :class:`ReconcileResult`:

- :attr:`ReconcileStatus.COMPLETED` — the effect definitely happened. The
  transition is marked completed with the returned ``result`` and the original
  dispatch returns that result (no re-execution).
- :attr:`ReconcileStatus.NOT_EXECUTED` — the effect definitely never happened.
  The transition is reset so the tool may execute exactly once.
- :attr:`ReconcileStatus.UNKNOWN` — still ambiguous. The transition hard-blocks
  for manual reconciliation, exactly as if no reconciler were present.

Reconciliation is fail-closed: if the reconciler raises (network error,
timeout, provider outage), the result is treated as ``UNKNOWN`` and the
transition hard-blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from mycelium._compat import StrEnum

if TYPE_CHECKING:
    from mycelium.action_ledger import LedgerEntry


class ReconcileStatus(StrEnum):
    """Outcome of reconciling an ambiguous transition against a provider."""

    COMPLETED = "COMPLETED"
    NOT_EXECUTED = "NOT_EXECUTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ReconcileResult:
    """The provider-confirmed disposition of an ambiguous transition."""

    status: ReconcileStatus
    result: Any = None

    @classmethod
    def completed(cls, result: Any = None) -> ReconcileResult:
        """The effect happened; ``result`` becomes the transition's result."""
        return cls(ReconcileStatus.COMPLETED, result)

    @classmethod
    def not_executed(cls) -> ReconcileResult:
        """The effect never happened; the transition may execute once."""
        return cls(ReconcileStatus.NOT_EXECUTED)

    @classmethod
    def unknown(cls) -> ReconcileResult:
        """Still ambiguous; the transition hard-blocks."""
        return cls(ReconcileStatus.UNKNOWN)


@runtime_checkable
class Reconciler(Protocol):
    """Read-only resolver for ambiguous side-effecting transitions.

    Implementations look up ``entry.external_operation_ref`` at the provider and
    return a :class:`ReconcileResult`. Implement :meth:`reconcile` for sync
    tools; optionally implement ``reconcile_async`` for async tools (the async
    claim path prefers it when present, otherwise falls back to
    :meth:`reconcile`).
    """

    def reconcile(self, entry: LedgerEntry) -> ReconcileResult:
        """Return the provider-confirmed disposition of ``entry``."""
        ...


__all__ = [
    "Reconciler",
    "ReconcileResult",
    "ReconcileStatus",
]
