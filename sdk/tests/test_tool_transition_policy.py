"""Tests for per-tool retry_permission and side_effect_boundary YAML."""

from __future__ import annotations

import pytest

from mycelium import (
    ActionLedger,
    ConfigError,
    InMemoryLedgerStorage,
    LedgerEntry,
    LedgerHardBlockError,
    RetryPermission,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    load_config_from_string,
)


def test_config_parses_retry_permission_and_boundary() -> None:
    yaml_text = """
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"

action_ledger:
  storage: memory
  tools:
    - send_payment

tools:
  send_payment:
    side_effect_class: keyed_mutate
    retry_permission: safe_retry
    side_effect_boundary: not_crossed
"""
    config = load_config_from_string(yaml_text)
    tool = config.tools["send_payment"]
    binding = config.tool_transition_binding(tool)

    assert tool.retry_permission == RetryPermission.SAFE_RETRY
    assert tool.side_effect_boundary == SideEffectBoundary.NOT_CROSSED
    assert binding is not None
    assert binding.retry_permission == RetryPermission.SAFE_RETRY
    assert binding.side_effect_boundary_default == SideEffectBoundary.NOT_CROSSED


def test_config_rejects_invalid_retry_permission() -> None:
    yaml_text = """
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"

tools:
  send_payment:
    side_effect_class: keyed_mutate
    retry_permission: retry_forever
"""
    with pytest.raises(ConfigError, match="retry_permission"):
        load_config_from_string(yaml_text)


def test_safe_retry_allows_payment_retry_after_failed_before_effect() -> None:
    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    yaml_text = """
transition:
  agent_id: demo
  policy_version: "1"

action_ledger:
  storage: memory
  tools:
    - send_payment

tools:
  send_payment:
    side_effect_class: keyed_mutate
    retry_permission: safe_retry
"""
    config = load_config_from_string(yaml_text)
    binding = config.tool_transition_binding(config.tools["send_payment"])
    assert binding is not None

    request_id = "pay-retry"
    storage.set(
        LedgerEntry(
            request_id=request_id,
            tool="send_payment",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.FAILED_BEFORE_EFFECT.value,
            side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
            idempotency_key=request_id,
        )
    )

    claimed = ledger.claim_side_effecting(
        request_id,
        "send_payment",
        (),
        {"amount": 1.0},
        binding,
    )
    assert claimed.terminal_outcome == TerminalOutcome.IN_FLIGHT.value


def test_crossed_boundary_hard_blocks_even_with_safe_retry() -> None:
    from mycelium.transition import ToolTransitionBinding

    storage = InMemoryLedgerStorage()
    ledger = ActionLedger(storage=storage)
    binding = ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.IDEMPOTENT_MUTATE,
        retry_permission=RetryPermission.SAFE_RETRY,
    )
    request_id = "crossed-fail"
    storage.set(
        LedgerEntry(
            request_id=request_id,
            tool="upsert_record",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.FAILED_BEFORE_EFFECT.value,
            side_effect_boundary=SideEffectBoundary.CROSSED.value,
            idempotency_key=request_id,
        )
    )

    with pytest.raises(LedgerHardBlockError, match="crossed"):
        ledger.claim_side_effecting(
            request_id,
            "upsert_record",
            (),
            {},
            binding,
        )
