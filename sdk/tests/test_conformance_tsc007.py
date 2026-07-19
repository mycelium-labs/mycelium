"""Conformance suite for Tuttotorna spec TSC-007 (transition sufficiency).

These five tests map 1:1 to Tuttotorna's canonical "transition-sufficiency"
cases (spec TSC-007, cross-referenced with langgraph#7417). Each case
sets up a prior transition in a given state, then has an agent attempt to
act again through the ledgered tool wrapper, and asserts the resolver makes
the decision TSC-007 requires.

The core promise proved by the suite is **must_not_execute_again**: when the
spec says the side effect must not fire again (cases 1, 2, 4, 5), the test
includes an explicit assertion that the side-effect body ran at most once
(an ``executions`` list that never grows, or a ``LedgerHardBlockError`` with
no second execution). Case 3 is the lone "re-execution is permitted" case
and asserts the body runs again.

No product code is modified by this suite. ``xfail`` is used only if a case
reveals a behavioral gap.
"""

from __future__ import annotations

import threading
import time

import pytest

from mycelium import (
    InMemoryLedgerStorage,
    LedgerEntry,
    LedgerHardBlockError,
    ReconcileResult,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    derive_transition_key_for_call,
    execution_scope,
    ledger_sync,
)
from mycelium.reconcile import Reconciler
from mycelium.transition_resolution import TransitionGate, resolve_side_effect_gate

# --- shared helpers ---------------------------------------------------------


def _payment_binding() -> ToolTransitionBinding:
    """Non-idempotent side effect (payment / write / email / subagent class)."""
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
    )


def _idempotent_binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.IDEMPOTENT_MUTATE,
    )


def _irreversible_binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.IRREVERSIBLE,
    )


def _scope() -> TransitionScope:
    return TransitionScope(thread_id="t1", run_id="r1")


# --- case 1 -----------------------------------------------------------------


# MIRRORS: test_side_effect_resolution.py::test_payment_polls_in_flight_instead_of_pending_error
# (the existing two-thread test asserts the same "side effect fires once"
# promise; this version pre-stages the in-flight entry and completes it from
# a watcher thread so the polling path is exercised in isolation).
def test_case_1_in_flight_valid_lease_polls_without_reexecuting() -> None:
    """TSC-007 case 1: prior transition IN_FLIGHT with a still-valid lease.

    A redispatching agent MUST wait / return the in-progress result and the
    side effect MUST NOT execute again.
    """
    storage = InMemoryLedgerStorage()
    binding = _payment_binding()
    executions: list[int] = []
    completed = threading.Event()

    @ledger_sync(
        storage=storage,
        transition_binding=binding,
        poll_interval=0.01,
        poll_timeout=2.0,
    )
    def send_payment(amount: float) -> dict[str, str]:
        executions.append(amount)
        return {"status": "sent", "amount": str(amount)}

    with execution_scope(_scope()):
        kwargs = {"amount": 10.0, "tool_call_id": "call_tsc007_c1"}
        request_id = derive_transition_key_for_call(
            "send_payment", (), kwargs, binding
        )
        # Prior transition: in-flight with a still-valid lease held by another
        # worker (boundary not crossed yet).
        storage.set(
            LedgerEntry(
                request_id=request_id,
                tool="send_payment",
                args=[],
                kwargs={"amount": 10.0},
                status="in-flight",
                terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
                side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
                lease_until=time.time() + 60.0,
                idempotency_key=request_id,
                owner="other-worker:1",
            )
        )

        # The other worker eventually completes the side effect.
        def other_worker() -> None:
            time.sleep(0.05)
            storage.set(
                LedgerEntry(
                    request_id=request_id,
                    tool="send_payment",
                    args=[],
                    kwargs={"amount": 10.0},
                    status="completed",
                    terminal_outcome=TerminalOutcome.COMPLETED.value,
                    side_effect_boundary=SideEffectBoundary.CROSSED.value,
                    result={"status": "sent", "amount": "10.0"},
                    lease_until=None,
                    idempotency_key=request_id,
                    owner="other-worker:1",
                )
            )
            completed.set()

        watcher = threading.Thread(target=other_worker)
        watcher.start()

        result = send_payment(amount=10.0, tool_call_id="call_tsc007_c1")

        watcher.join(timeout=2.0)

    assert completed.is_set()
    # must_not_execute_again: redispatching worker polled and returned the
    # other worker's result; the side-effect body never ran here.
    assert executions == []
    assert result == {"status": "sent", "amount": "10.0"}


# --- case 2 -----------------------------------------------------------------


# NEW: not previously asserted.
# ``claim_side_effecting`` has a RETURN branch for COMPLETED, but no existing
# test drives a *live* redispatch through the ``@ledger_sync`` wrapper against
# a pre-stored COMPLETED side-effecting entry and asserts the body does not
# re-execute. (Existing COMPLETED redispatch tests cover the read-only path
# and the reconcile path, not this side-effecting direct-return path.)
def test_case_2_prior_completed_returns_stored_result_without_reexecuting() -> None:
    """TSC-007 case 2: prior transition COMPLETED.

    The caller MUST return the stored receipt/result and the side effect
    MUST NOT execute again.
    """
    storage = InMemoryLedgerStorage()
    binding = _payment_binding()
    executions: list[float] = []

    @ledger_sync(storage=storage, transition_binding=binding)
    def send_payment(amount: float) -> dict[str, str]:
        executions.append(amount)
        return {"status": "sent", "amount": str(amount)}

    with execution_scope(_scope()):
        kwargs = {"amount": 25.0, "tool_call_id": "call_tsc007_c2"}
        request_id = derive_transition_key_for_call(
            "send_payment", (), kwargs, binding
        )
        storage.set(
            LedgerEntry(
                request_id=request_id,
                tool="send_payment",
                args=[],
                kwargs={"amount": 25.0},
                status="completed",
                terminal_outcome=TerminalOutcome.COMPLETED.value,
                side_effect_boundary=SideEffectBoundary.CROSSED.value,
                result={"status": "sent", "amount": "25.0"},
                lease_until=None,
                idempotency_key=request_id,
            )
        )

        result = send_payment(amount=25.0, tool_call_id="call_tsc007_c2")

    # must_not_execute_again: body never ran; stored receipt returned as-is.
    assert executions == []
    assert result == {"status": "sent", "amount": "25.0"}

    entry = storage.get(request_id)
    assert entry is not None
    assert entry.terminal_outcome == TerminalOutcome.COMPLETED.value


# --- case 3 -----------------------------------------------------------------


# MIRRORS: test_side_effect_resolution.py::test_idempotent_write_retries_failed_before_effect
# (covers FAILED_BEFORE_EFFECT + safe-retry binding → re-execution permitted).
# Also exercises the resolver-level gate directly to pin the ALLOW decision.
def test_case_3_failed_before_effect_retry_allowed_reexecutes() -> None:
    """TSC-007 case 3: FAILED before the side-effect boundary was crossed.

    For an idempotent tool whose retry permission is ``SAFE_RETRY``, the
    resolver MUST allow a retry and re-execution IS permitted.
    """
    storage = InMemoryLedgerStorage()
    binding = _idempotent_binding()
    attempts = {"count": 0}

    @ledger_sync(storage=storage, transition_binding=binding)
    def upsert_record(record_id: str) -> dict[str, str]:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise RuntimeError("transient")
        return {"record_id": record_id, "status": "upserted"}

    # Resolver-level guarantee: a FAILED_BEFORE_EFFECT entry against an
    # idempotent / safe-retry binding decides ALLOW (retry-allowed).
    failed_entry = LedgerEntry(
        request_id="x",
        tool="upsert_record",
        args=[],
        kwargs={},
        status="failed",
        terminal_outcome=TerminalOutcome.FAILED_BEFORE_EFFECT.value,
        side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
        error="RuntimeError: transient",
        idempotency_key="x",
    )
    assert (
        resolve_side_effect_gate(failed_entry, binding)
        == TransitionGate.ALLOW
    )

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            upsert_record(record_id="r1", tool_call_id="call_tsc007_c3")
        result = upsert_record(record_id="r1", tool_call_id="call_tsc007_c3")

    # Re-execution is permitted: the body ran twice (failed try + retry).
    assert attempts["count"] == 2
    assert result == {"record_id": "r1", "status": "upserted"}


# --- case 4 -----------------------------------------------------------------


# MIRRORS: test_side_effect_resolution.py::test_payment_hard_blocks_failed_after_effect_retry
# (covers the FAILED_AFTER_EFFECT hard-block on a NON_IDEMPOTENT_MUTATE
# binding). Mirrored here through the live ``@ledger_sync`` wrapper with an
# ``IRREVERSIBLE`` (onchain_action) binding to prove no second execution
# occurs out of the wrapper path.
def test_case_4_failed_after_effect_hard_blocks_no_reexecuting() -> None:
    """TSC-007 case 4: FAILED after the side effect, outcome UNKNOWN,
    irreversible / non-idempotent class.

    The resolver MUST HARD_BLOCK and the side effect MUST NOT execute again.
    """
    storage = InMemoryLedgerStorage()
    binding = _irreversible_binding()
    executions: list[str] = []

    @ledger_sync(storage=storage, transition_binding=binding)
    def onchain_action(amount: int) -> dict[str, str]:
        executions.append("executed")
        return {"tx": "0xabc", "amount": str(amount)}

    with execution_scope(_scope()):
        kwargs = {"amount": 5, "tool_call_id": "call_tsc007_c4"}
        request_id = derive_transition_key_for_call(
            "onchain_action", (), kwargs, binding
        )
        storage.set(
            LedgerEntry(
                request_id=request_id,
                tool="onchain_action",
                args=[],
                kwargs={"amount": 5},
                status="failed",
                terminal_outcome=TerminalOutcome.FAILED_AFTER_EFFECT.value,
                side_effect_boundary=SideEffectBoundary.CROSSED.value,
                error="RuntimeError: broadcast but no receipt",
                external_operation_ref="chain:0xabc",
                lease_until=None,
                idempotency_key=request_id,
            )
        )

        with pytest.raises(LedgerHardBlockError, match="manual reconciliation"):
            onchain_action(amount=5, tool_call_id="call_tsc007_c4")

    # must_not_execute_again: hard-blocked without invoking the body.
    assert executions == []
    entry = storage.get(request_id)
    assert entry is not None
    assert entry.terminal_outcome == TerminalOutcome.FAILED_AFTER_EFFECT.value


# --- case 5 -----------------------------------------------------------------


class _FakeReconciler:
    """Minimal Reconciler test double (sync path) for TSC-007 case 5."""

    def __init__(self, result: ReconcileResult) -> None:
        self._result = result
        self.calls: list[str] = []

    def reconcile(self, entry) -> ReconcileResult:  # noqa: ANN001
        self.calls.append(entry.request_id)
        return self._result


# MIRRORS: test_reconcile.py::test_reconcile_completed_returns_result_without_reexec
# (drives the reconcile path with a fake Reconciler). Adds the explicit
# "no blind re-execution of the side-effect body" assertion and verifies the
# ``UNKNOWN`` outcome on a payment/write/email/subagent (NON_IDEMPOTENT_MUTATE)
# class is reconciled, not blindly re-executed.
def test_case_5_unknown_terminal_outcome_reconciles_not_reexecutes() -> None:
    """TSC-007 case 5: terminal outcome UNKNOWN for a
    payment/write/email/subagent (non-idempotent) class.

    The resolver MUST hard-block OR trigger a reconcile; it MUST NOT blindly
    re-execute the side effect. Drives the reconcile path with a fake
    ``Reconciler`` test double returning ``COMPLETED`` — the original
    dispatch returns the reconciled result with the body never running again.
    """
    storage = InMemoryLedgerStorage()
    binding = _payment_binding()
    reconciler = _FakeReconciler(ReconcileResult.completed({"status": "sent"}))
    executions: list[float] = []

    @ledger_sync(
        storage=storage,
        transition_binding=binding,
        reconciler=reconciler,
    )
    def send_email(to: str) -> dict[str, str]:
        executions.append(1)
        return {"status": "sent", "to": to}

    with execution_scope(_scope()):
        kwargs = {"to": "ops@example.com", "tool_call_id": "call_tsc007_c5"}
        request_id = derive_transition_key_for_call(
            "send_email", (), kwargs, binding
        )
        storage.set(
            LedgerEntry(
                request_id=request_id,
                tool="send_email",
                args=[],
                kwargs={"to": "ops@example.com"},
                status="in-flight",
                terminal_outcome=TerminalOutcome.UNKNOWN.value,
                side_effect_boundary=SideEffectBoundary.MAYBE_CROSSED.value,
                error="provider timeout after send",
                external_operation_ref="provider:msg_123",
                lease_until=None,
                idempotency_key=request_id,
            )
        )

        # Resolver-level guarantee: an UNKNOWN outcome hard-blocks at the gate.
        existing = storage.get(request_id)
        assert existing is not None
        assert (
            resolve_side_effect_gate(existing, binding)
            == TransitionGate.HARD_BLOCK
        )

        # The reconcile path turns the hard-block into a recovered COMPLETED
        # transition — the side-effect body MUST NOT re-execute.
        result = send_email(to="ops@example.com", tool_call_id="call_tsc007_c5")

    assert result == {"status": "sent"}
    # must_not_execute_again: reconcile drove the recovery; body never ran.
    assert executions == []
    assert len(reconciler.calls) == 1
    assert reconciler.calls[0] == request_id

    entry = storage.get(request_id)
    assert entry is not None
    assert entry.terminal_outcome == TerminalOutcome.COMPLETED.value
    assert entry.result == {"status": "sent"}


# Reconciler is referenced for the type assertion / spec linkage below
# (acts as a contract anchor for ``_FakeReconciler`` conforming to the
# ``Reconciler`` Protocol; imported here to keep the dependency explicit).
_ = Reconciler