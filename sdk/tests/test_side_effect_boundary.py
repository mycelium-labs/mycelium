"""Tests for the mid-flight side-effect boundary marker (v1.5.0).

Covers the ``side_effect()`` context manager / ``mark_*`` API and the
boundary-aware failure classification it drives:

    not_crossed   -> FAILED_BEFORE_EFFECT
    maybe_crossed -> UNKNOWN
    crossed       -> FAILED_AFTER_EFFECT
"""

from __future__ import annotations

import pytest

from mycelium import (
    ActionLedger,
    InMemoryLedgerStorage,
    LedgerHardBlockError,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    execution_scope,
    ledger,
    ledger_sync,
    mark_crossed,
    mark_maybe_crossed,
    side_effect,
)
from mycelium.action_ledger import get_active_transition


def _binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
    )


def test_advance_boundary_is_monotonic() -> None:
    storage = InMemoryLedgerStorage()
    action_ledger = ActionLedger(storage=storage)
    rid = "rid-monotonic"
    action_ledger.claim_side_effecting(rid, "t", (), {}, _binding())

    action_ledger.advance_boundary(rid, SideEffectBoundary.MAYBE_CROSSED)
    assert (
        storage.get(rid).side_effect_boundary == SideEffectBoundary.MAYBE_CROSSED.value
    )

    # Regressing is a no-op.
    action_ledger.advance_boundary(rid, SideEffectBoundary.NOT_CROSSED)
    assert (
        storage.get(rid).side_effect_boundary == SideEffectBoundary.MAYBE_CROSSED.value
    )

    action_ledger.advance_boundary(rid, SideEffectBoundary.CROSSED)
    assert storage.get(rid).side_effect_boundary == SideEffectBoundary.CROSSED.value

    # Cannot fall back from crossed.
    action_ledger.advance_boundary(rid, SideEffectBoundary.MAYBE_CROSSED)
    assert storage.get(rid).side_effect_boundary == SideEffectBoundary.CROSSED.value


def test_side_effect_marks_maybe_crossed_midflight() -> None:
    storage = InMemoryLedgerStorage()
    seen: dict[str, str] = {}

    @ledger_sync(storage=storage, transition_binding=_binding())
    def charge(amount: float) -> dict[str, str]:
        active = get_active_transition()
        assert active is not None
        with side_effect():
            seen["during"] = storage.get(active.request_id).side_effect_boundary
        return {"ok": True}

    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        charge(amount=10.0, tool_call_id="c1")

    assert seen["during"] == SideEffectBoundary.MAYBE_CROSSED.value


def test_exception_inside_side_effect_marks_unknown_and_hard_blocks() -> None:
    storage = InMemoryLedgerStorage()
    rids: list[str] = []

    @ledger_sync(storage=storage, transition_binding=_binding())
    def charge(amount: float) -> dict[str, str]:
        active = get_active_transition()
        assert active is not None
        rids.append(active.request_id)
        with side_effect():
            raise RuntimeError("provider timeout mid-call")

    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        with pytest.raises(RuntimeError):
            charge(amount=10.0, tool_call_id="c1")

        entry = storage.get(rids[0])
        assert entry.resolved_terminal_outcome() == TerminalOutcome.UNKNOWN
        assert entry.side_effect_boundary == SideEffectBoundary.MAYBE_CROSSED.value

        # Redispatch of the same transition must hard-block, not re-execute.
        with pytest.raises(LedgerHardBlockError):
            charge(amount=10.0, tool_call_id="c1")


def test_exception_before_marker_is_failed_before_effect() -> None:
    storage = InMemoryLedgerStorage()
    rids: list[str] = []

    @ledger_sync(storage=storage, transition_binding=_binding())
    def charge(amount: float) -> dict[str, str]:
        active = get_active_transition()
        assert active is not None
        rids.append(active.request_id)
        raise ValueError("bad input before any external call")

    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        with pytest.raises(ValueError):
            charge(amount=10.0, tool_call_id="c1")

    entry = storage.get(rids[0])
    assert entry.resolved_terminal_outcome() == TerminalOutcome.FAILED_BEFORE_EFFECT
    assert entry.side_effect_boundary == SideEffectBoundary.NOT_CROSSED.value


def test_mark_crossed_then_exception_is_failed_after_effect() -> None:
    storage = InMemoryLedgerStorage()
    rids: list[str] = []

    @ledger_sync(storage=storage, transition_binding=_binding())
    def charge(amount: float) -> dict[str, str]:
        active = get_active_transition()
        assert active is not None
        rids.append(active.request_id)
        mark_crossed()  # effect confirmed applied
        raise RuntimeError("failed while parsing provider response")

    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        with pytest.raises(RuntimeError):
            charge(amount=10.0, tool_call_id="c1")

    entry = storage.get(rids[0])
    assert entry.resolved_terminal_outcome() == TerminalOutcome.FAILED_AFTER_EFFECT
    assert entry.side_effect_boundary == SideEffectBoundary.CROSSED.value


def test_marker_outside_tool_warns_and_is_noop() -> None:
    with pytest.warns(UserWarning, match="outside a ledgered tool"):
        mark_maybe_crossed()


def test_async_side_effect_marks_unknown_on_error() -> None:
    import asyncio

    storage = InMemoryLedgerStorage()
    rids: list[str] = []

    @ledger(storage=storage, transition_binding=_binding())
    async def charge(amount: float) -> dict[str, str]:
        active = get_active_transition()
        assert active is not None
        rids.append(active.request_id)
        with side_effect():
            raise RuntimeError("async provider timeout")

    async def run() -> None:
        with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
            with pytest.raises(RuntimeError):
                await charge(amount=10.0, tool_call_id="c1")

    asyncio.run(run())

    entry = storage.get(rids[0])
    assert entry.resolved_terminal_outcome() == TerminalOutcome.UNKNOWN
    assert entry.side_effect_boundary == SideEffectBoundary.MAYBE_CROSSED.value
