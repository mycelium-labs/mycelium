"""Tests for provider idempotency key enforcement (v1.8.0).

Opt-in via ``provider_idempotency_key_param``. When a keyed/idempotent tool
declares which kwarg carries the provider idempotency key, a
``retry_only_with_same_provider_idempotency_key`` retry is allowed only when it
provably reuses the stored key; otherwise it hard-blocks. Without the
declaration the permission stays cooperative (unchanged, backward compatible).
"""

from __future__ import annotations

import pytest

from mycelium import (
    InMemoryLedgerStorage,
    LedgerEntry,
    LedgerHardBlockError,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    derive_transition_key_for_call,
    execution_scope,
    ledger_sync,
    load_config_from_string,
)
from mycelium.transition_resolution import TransitionGate, resolve_side_effect_gate


def _keyed_binding(*, enforce: bool) -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.KEYED_MUTATE,
        provider_idempotency_key_param="idempotency_key" if enforce else None,
    )


def _failed_before(provider_key: str | None) -> LedgerEntry:
    return LedgerEntry(
        request_id="x",
        tool="send_payment",
        args=[],
        kwargs={},
        status="failed",
        terminal_outcome=TerminalOutcome.FAILED_BEFORE_EFFECT.value,
        side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
        provider_idempotency_key=provider_key,
    )


def _scope() -> TransitionScope:
    return TransitionScope(thread_id="t1", run_id="r1")


# --- gate-level enforcement ------------------------------------------------


def test_gate_allows_same_provider_key() -> None:
    gate = resolve_side_effect_gate(
        _failed_before("k1"),
        _keyed_binding(enforce=True),
        incoming_provider_idempotency_key="k1",
    )
    assert gate == TransitionGate.ALLOW


def test_gate_hard_blocks_different_provider_key() -> None:
    gate = resolve_side_effect_gate(
        _failed_before("k1"),
        _keyed_binding(enforce=True),
        incoming_provider_idempotency_key="k2",
    )
    assert gate == TransitionGate.HARD_BLOCK


def test_gate_hard_blocks_missing_incoming_key() -> None:
    gate = resolve_side_effect_gate(
        _failed_before("k1"),
        _keyed_binding(enforce=True),
        incoming_provider_idempotency_key=None,
    )
    assert gate == TransitionGate.HARD_BLOCK


def test_gate_hard_blocks_missing_stored_key() -> None:
    gate = resolve_side_effect_gate(
        _failed_before(None),
        _keyed_binding(enforce=True),
        incoming_provider_idempotency_key="k1",
    )
    assert gate == TransitionGate.HARD_BLOCK


def test_gate_without_enforcement_stays_cooperative_allow() -> None:
    # No provider_idempotency_key_param: behavior unchanged from pre-1.8.0.
    gate = resolve_side_effect_gate(
        _failed_before(None),
        _keyed_binding(enforce=False),
    )
    assert gate == TransitionGate.ALLOW


# --- transition-key stability ----------------------------------------------


def test_declared_key_is_excluded_from_transition_key() -> None:
    binding = _keyed_binding(enforce=True)
    with execution_scope(_scope()):
        k1 = derive_transition_key_for_call(
            "send_payment",
            (),
            {"amount": 10.0, "idempotency_key": "k1", "tool_call_id": "c1"},
            binding,
        )
        k2 = derive_transition_key_for_call(
            "send_payment",
            (),
            {"amount": 10.0, "idempotency_key": "k2", "tool_call_id": "c1"},
            binding,
        )
    assert k1 == k2  # same intent, different key -> same transition


def test_undeclared_key_changes_transition_key() -> None:
    binding = _keyed_binding(enforce=False)
    with execution_scope(_scope()):
        k1 = derive_transition_key_for_call(
            "send_payment",
            (),
            {"amount": 10.0, "idempotency_key": "k1", "tool_call_id": "c1"},
            binding,
        )
        k2 = derive_transition_key_for_call(
            "send_payment",
            (),
            {"amount": 10.0, "idempotency_key": "k2", "tool_call_id": "c1"},
            binding,
        )
    assert k1 != k2


# --- end-to-end through the ledger -----------------------------------------


def test_retry_with_same_provider_key_is_allowed() -> None:
    storage = InMemoryLedgerStorage()
    binding = _keyed_binding(enforce=True)
    attempts = {"n": 0}

    @ledger_sync(storage=storage, transition_binding=binding)
    def send_payment(amount: float, idempotency_key: str) -> dict[str, str]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("gateway timeout before charge")
        return {"status": "sent"}

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            send_payment(amount=10.0, idempotency_key="k1", tool_call_id="c1")

        result = send_payment(amount=10.0, idempotency_key="k1", tool_call_id="c1")

    assert attempts["n"] == 2
    assert result == {"status": "sent"}


def test_retry_with_different_provider_key_hard_blocks() -> None:
    storage = InMemoryLedgerStorage()
    binding = _keyed_binding(enforce=True)
    attempts = {"n": 0}

    @ledger_sync(storage=storage, transition_binding=binding)
    def send_payment(amount: float, idempotency_key: str) -> dict[str, str]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("gateway timeout before charge")
        return {"status": "sent"}

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            send_payment(amount=10.0, idempotency_key="k1", tool_call_id="c1")

        with pytest.raises(LedgerHardBlockError):
            send_payment(amount=10.0, idempotency_key="k2", tool_call_id="c1")

    assert attempts["n"] == 1  # second body never executed


def test_claim_stores_provider_idempotency_key() -> None:
    from mycelium import ActionLedger

    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    binding = _keyed_binding(enforce=True)

    with execution_scope(_scope()):
        entry = ledger.claim_side_effecting(
            derive_transition_key_for_call(
                "send_payment",
                (),
                {"amount": 10.0, "idempotency_key": "k1", "tool_call_id": "c1"},
                binding,
            ),
            "send_payment",
            (),
            {"amount": 10.0, "idempotency_key": "k1"},
            binding,
        )

    assert entry.provider_idempotency_key == "k1"


# --- config wiring ---------------------------------------------------------


def test_config_parses_and_binds_provider_key_param() -> None:
    yaml_text = """
transition:
  agent_id: demo
  policy_version: "1"
action_ledger:
  storage: memory
  tools: [send_payment]
tools:
  send_payment:
    side_effect_class: keyed_mutate
    provider_idempotency_key_param: idempotency_key
"""
    config = load_config_from_string(yaml_text)
    tool_config = config.tools["send_payment"]
    assert tool_config.provider_idempotency_key_param == "idempotency_key"

    binding = config.tool_transition_binding(tool_config)
    assert binding is not None
    assert binding.provider_idempotency_key_param == "idempotency_key"


def test_config_rejects_non_string_provider_key_param() -> None:
    from mycelium import ConfigError

    yaml_text = """
transition:
  agent_id: demo
  policy_version: "1"
tools:
  send_payment:
    side_effect_class: keyed_mutate
    provider_idempotency_key_param: 123
"""
    with pytest.raises(ConfigError, match="provider_idempotency_key_param"):
        load_config_from_string(yaml_text)


def test_entry_round_trips_provider_idempotency_key() -> None:
    entry = LedgerEntry(
        request_id="r1",
        tool="send_payment",
        args=[],
        kwargs={},
        status="in-flight",
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        provider_idempotency_key="k1",
    )
    restored = LedgerEntry.from_dict(entry.to_dict())
    assert restored.provider_idempotency_key == "k1"

    legacy = {
        "request_id": "r1",
        "tool": "send_payment",
        "args": [],
        "kwargs": {},
        "status": "completed",
        "terminal_outcome": TerminalOutcome.COMPLETED.value,
    }
    assert LedgerEntry.from_dict(legacy).provider_idempotency_key is None
