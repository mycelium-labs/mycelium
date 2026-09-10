"""Execution context variables and boundary markers for the action ledger."""

from __future__ import annotations

import logging
import threading
import warnings
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mycelium.ledger_model import (
    DEFAULT_LEASE_RENEW_RATIO,
    MIN_LEASE_RENEW_INTERVAL,
    LedgerError,
)
from mycelium.transition import SideEffectBoundary

if TYPE_CHECKING:
    from mycelium.action_ledger import ActionLedger
    from mycelium.transition import ToolTransitionBinding

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ActiveTransition:
    """The side-effecting transition currently executing on this task/thread."""

    ledger: ActionLedger
    request_id: str
    binding: ToolTransitionBinding | None
    call_kwargs: Mapping[str, Any]
    owner: str | None
    fence: int


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
    active.ledger.advance_boundary(
        active.request_id,
        boundary,
        expected_owner=active.owner,
        expected_fence=active.fence,
    )


def mark_maybe_crossed() -> None:
    """Mark the active transition as ``maybe_crossed``.

    Call immediately before performing the external operation. If the tool
    raises or the process crashes after this point, the durable entry retains
    ``maybe_crossed`` so a redispatch hard-blocks instead of re-executing a
    possibly-already-applied side effect.

    Time-bounded authority and use-time currency are re-validated here
    (use phase) before the boundary advances. Expired authority or a
    stale/changed fact hard-blocks and never marks ``maybe_crossed``.
    """
    from mycelium.composite import get_active_composite
    from mycelium.use_time_currency import enforce_use_boundary

    active = _active_transition_var.get()
    enforce_use_boundary(kwargs=active.call_kwargs if active is not None else {})
    composite = get_active_composite()
    if composite is not None:
        composite.validate_boundary()
    _advance_active_boundary(SideEffectBoundary.MAYBE_CROSSED)


async def mark_maybe_crossed_async() -> None:
    """Asynchronously validate and mark the active transition as ``maybe_crossed``."""
    from mycelium.composite import get_active_composite
    from mycelium.use_time_currency import enforce_use_boundary_async

    active = _active_transition_var.get()
    await enforce_use_boundary_async(kwargs=active.call_kwargs if active is not None else {})
    composite = get_active_composite()
    if composite is not None:
        composite.validate_boundary()
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
    active.ledger.attach_external_operation_ref(
        active.request_id,
        ref,
        expected_owner=active.owner,
        expected_fence=active.fence,
    )


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
    active.ledger.renew_lease(
        active.request_id,
        lease_ttl=lease_ttl,
        _expected_owner=active.owner,
        _expected_fence=active.fence,
    )


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
def _lease_auto_renew(
    ledger: ActionLedger,
    request_id: str,
    *,
    tool: str | None = None,
    owner: str | None,
    fence: int,
) -> Iterator[None]:
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

    def _emit_renewal_failure(exc: Exception) -> None:
        event_tool = tool
        if event_tool is None:
            try:
                entry = ledger.get(request_id)
                event_tool = entry.tool if entry is not None else "unknown"
            except Exception:
                event_tool = "unknown"
        try:
            ledger._emit_outcome(
                request_id=request_id,
                tool=event_tool,
                event="lease_renewal_failure",
                error_class=type(exc).__name__,
                owner=owner,
            )
        except Exception:
            _logger.exception(
                "could not emit lease-renewal failure for %s",
                request_id,
            )

    def _loop() -> None:
        while not stop.wait(interval):
            try:
                ledger.renew_lease(
                    request_id,
                    lease_ttl=ledger._lease_ttl,
                    _expected_owner=owner,
                    _expected_fence=fence,
                )
            except LedgerError as exc:
                _emit_renewal_failure(exc)
                _logger.warning(
                    "lease auto-renew stopped for %s: %s",
                    request_id,
                    exc,
                )
                return
            except Exception as exc:
                _emit_renewal_failure(exc)
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

    Use-time authority expiry is enforced inside :func:`mark_maybe_crossed`
    immediately before the boundary advances — after leases, queues, and
    backoff, and before any provider call.
    """
    mark_maybe_crossed()
    yield
    mark_crossed()


@asynccontextmanager
async def side_effect_async() -> AsyncIterator[None]:
    """Wrap an async tool's external operation with async final validation."""
    await mark_maybe_crossed_async()
    yield
    mark_crossed()
