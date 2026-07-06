"""Tests for transition envelope and rich transition keys."""

from __future__ import annotations

import pytest

from mycelium import (
    ConfigError,
    InMemoryLedgerStorage,
    SideEffectClass,
    ToolTransitionBinding,
    TransitionScope,
    derive_transition_key_for_call,
    execution_scope,
    get_ledger,
    ledger_sync,
    load_config_from_string,
)
from mycelium.transition import build_transition_preimage


def _binding(**overrides: object) -> ToolTransitionBinding:
    side_effect_class = overrides.pop("side_effect_class", SideEffectClass.PAYMENT)
    return ToolTransitionBinding.for_tool(
        agent_id=str(overrides.pop("agent_id", "payment-agent")),
        policy_version=str(overrides.pop("policy_version", "2026.07.1")),
        side_effect_class=side_effect_class,  # type: ignore[arg-type]
        **overrides,  # type: ignore[arg-type]
    )


def test_tool_call_id_is_in_preimage_when_present() -> None:
    preimage = build_transition_preimage(
        scope=TransitionScope(thread_id="t1", run_id="r1", node="pay"),
        dispatch_id="call_abc",
        tool="send_payment",
        args=(100.0,),
        kwargs={"recipient": "acct_1", "tool_call_id": "call_abc"},
        side_effect_class=SideEffectClass.PAYMENT,
        agent_id="payment-agent",
        policy_version="2026.07.1",
    )
    assert preimage["dispatch_id"] == "call_abc"
    assert preimage["schema"] == "mycelium.transition/v1"


def test_same_inputs_produce_same_transition_key() -> None:
    binding = _binding()
    kwargs = {"amount": 100.0, "recipient": "acct_1", "tool_call_id": "call_1"}
    with execution_scope(TransitionScope(thread_id="thread-1", run_id="run-1")):
        key_a = derive_transition_key_for_call(
            "send_payment", (), kwargs, binding
        )
        key_b = derive_transition_key_for_call(
            "send_payment", (), kwargs, binding
        )
    assert key_a == key_b
    assert len(key_a) == 64


def test_different_tool_call_id_produces_different_key() -> None:
    binding = _binding()
    base = {"amount": 100.0, "recipient": "acct_1"}
    with execution_scope(TransitionScope(thread_id="thread-1", run_id="run-1")):
        key_a = derive_transition_key_for_call(
            "send_payment",
            (),
            {**base, "tool_call_id": "call_1"},
            binding,
        )
        key_b = derive_transition_key_for_call(
            "send_payment",
            (),
            {**base, "tool_call_id": "call_2"},
            binding,
        )
    assert key_a != key_b


def test_policy_version_change_rotates_transition_key() -> None:
    kwargs = {"amount": 1.0, "tool_call_id": "call_1"}
    scope = TransitionScope(thread_id="t1", run_id="r1")
    with execution_scope(scope):
        key_v1 = derive_transition_key_for_call(
            "send_payment",
            (),
            kwargs,
            _binding(policy_version="2026.07.1"),
        )
        key_v2 = derive_transition_key_for_call(
            "send_payment",
            (),
            kwargs,
            _binding(policy_version="2026.08.1"),
        )
    assert key_v1 != key_v2


def test_side_effect_class_affects_transition_key() -> None:
    kwargs = {"amount": 1.0, "tool_call_id": "call_1"}
    with execution_scope(TransitionScope(thread_id="t1", run_id="r1")):
        payment = derive_transition_key_for_call(
            "send_payment",
            (),
            kwargs,
            _binding(side_effect_class=SideEffectClass.PAYMENT),
        )
        read_only = derive_transition_key_for_call(
            "send_payment",
            (),
            kwargs,
            _binding(side_effect_class=SideEffectClass.READ_ONLY),
        )
    assert payment != read_only


def test_ledger_deduplicates_by_transition_key() -> None:
    binding = _binding(side_effect_class=SideEffectClass.SUBAGENT)
    executions: list[int] = []

    @ledger_sync(storage=InMemoryLedgerStorage(), transition_binding=binding)
    def subagent_task(task: str) -> dict[str, str]:
        executions.append(1)
        return {"task": task, "result": "done"}

    with execution_scope(TransitionScope(thread_id="thread-1", run_id="run-1")):
        r1 = subagent_task(task="analyze", tool_call_id="call_subagent_1")
        r2 = subagent_task(task="analyze", tool_call_id="call_subagent_1")

    assert len(executions) == 1
    assert r1 == r2


def test_config_parses_transition_and_side_effect_class() -> None:
    yaml_text = """
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"
  scope_from:
    thread_id: thread_id

action_ledger:
  storage: memory
  tools:
    - send_payment

tools:
  send_payment:
    side_effect_class: payment
"""
    config = load_config_from_string(yaml_text)
    assert config.transition is not None
    assert config.transition.agent_id == "payment-agent"
    assert config.tools["send_payment"].side_effect_class == SideEffectClass.PAYMENT


def test_config_requires_side_effect_class_when_transition_and_ledger() -> None:
    yaml_text = """
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"

action_ledger:
  storage: memory
  tools:
    - send_payment

tools:
  send_payment: {}
"""
    with pytest.raises(ConfigError, match="side_effect_class"):
        load_config_from_string(yaml_text)


def test_audit_receipt_reads_agent_id_from_transition() -> None:
    yaml_text = """
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"

audit_receipt:
  signing_key: test-key
  storage: memory
"""
    config = load_config_from_string(yaml_text)
    audit = config.build_audit_receipt()
    assert audit is not None
    assert audit.agent_id == "payment-agent"


def test_config_parses_transition_timing_knobs() -> None:
    yaml_text = """
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"
  lease_ttl: 120
  poll_interval: 0.1
  poll_timeout: 30

action_ledger:
  storage: memory
  tools:
    - search_docs

tools:
  search_docs:
    side_effect_class: read_only
"""
    config = load_config_from_string(yaml_text)
    assert config.transition is not None
    assert config.transition.lease_ttl == 120.0
    assert config.transition.poll_interval == 0.1
    assert config.transition.poll_timeout == 30.0
    assert config._ledger_timing_kwargs() == {
        "lease_ttl": 120.0,
        "poll_interval": 0.1,
        "poll_timeout": 30.0,
    }


def test_config_rejects_invalid_transition_timing() -> None:
    yaml_text = """
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"
  lease_ttl: 0
"""
    with pytest.raises(ConfigError, match="lease_ttl"):
        load_config_from_string(yaml_text)


def test_yaml_timing_applied_to_read_only_ledger() -> None:
    yaml_text = """
transition:
  agent_id: demo-agent
  policy_version: "2026.07.1"
  lease_ttl: 0.05
  poll_interval: 0.02
  poll_timeout: 0.2

action_ledger:
  storage: memory
  tools:
    - search_docs

tools:
  search_docs:
    side_effect_class: read_only
"""
    config = load_config_from_string(yaml_text)

    @config.apply
    def search_docs(query: str) -> dict[str, str]:
        return {"query": query}

    ledger_instance = get_ledger(search_docs)
    assert ledger_instance is not None
    assert ledger_instance._lease_ttl == 0.05
    assert ledger_instance._poll_interval == 0.02
    assert ledger_instance._poll_timeout == 0.2


def test_legacy_audit_receipt_agent_id_rejected() -> None:
    yaml_text = """
audit_receipt:
  agent_id: legacy-agent
  signing_key: test-key
  storage: memory
"""
    with pytest.raises(ConfigError, match="transition.agent_id"):
        load_config_from_string(yaml_text)
