"""Tests for the YAML config loader."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from mycelium import (
    ConfigError,
    ToolBoundaryError,
    load_config,
    load_config_from_string,
)
from mycelium.tool_registry import ToolRegistry
from mycelium.tool_runner import ToolRunner

SAMPLE_YAML = """
tools:
  fetch_customer:
    protect:
      entity_param: customer_id
      ttl: 60
    bounded:
      schema:
        customer_id:
          type: string
          required: true
          pattern: "^c\\\\d+$"
      output_schema:
        customer_id:
          type: string
          required: true
        name:
          type: string
          required: true
      allowed_paths:
        - /workspace/src/
      path_param: path

  search_docs:
    bounded:
      schema:
        query:
          type: string
          required: true

registry:
  allowed:
    - fetch_customer
    - search_docs

runner:
  max_llm_retries: 2
  max_tool_retries: 3

history_guard:
  max_tokens: 100000
  max_messages: 1000

message_validator:
  enabled: true
"""


def test_load_config_parses_tools_and_registry() -> None:
    config = load_config_from_string(SAMPLE_YAML)

    assert "fetch_customer" in config.tools
    assert "search_docs" in config.tools
    assert config.registry_allowed == ["fetch_customer", "search_docs"]

    fetch = config.tools["fetch_customer"]
    assert fetch.protect == {"entity_param": "customer_id", "ttl": 60}
    assert fetch.bounded is not None
    assert "customer_id" in fetch.bounded["schema"]


async def test_apply_bounds_async_tool() -> None:
    config = load_config_from_string(SAMPLE_YAML)

    @config.apply
    async def fetch_customer(customer_id: str) -> dict[str, str]:
        return {"customer_id": customer_id, "name": "Alice"}

    # Valid call passes through.
    result = await fetch_customer(customer_id="c123")
    assert result == {"customer_id": "c123", "name": "Alice"}

    # Invalid pattern is caught.
    with pytest.raises(ToolBoundaryError) as exc_info:
        await fetch_customer(customer_id="bad-id")
    assert exc_info.value.violation == "pattern_mismatch"


def test_apply_bounds_sync_tool() -> None:
    config = load_config_from_string(SAMPLE_YAML)

    @config.apply
    def search_docs(query: str) -> list[str]:
        return [query]

    assert search_docs(query="billing") == ["billing"]

    with pytest.raises(ToolBoundaryError) as exc_info:
        search_docs(query=None)  # type: ignore[arg-type]
    assert exc_info.value.violation == "type_mismatch"


async def test_apply_leaves_unknown_tools_unchanged() -> None:
    config = load_config_from_string(SAMPLE_YAML)

    @config.apply
    async def unrelated(x: int) -> int:
        return x * 2

    assert await unrelated(3) == 6


async def test_apply_orders_bounded_outside_protect() -> None:
    """Validation should run before the cache lookup."""
    config = load_config_from_string(SAMPLE_YAML)

    calls: list[str] = []

    @config.apply
    async def fetch_customer(customer_id: str) -> dict[str, str]:
        calls.append("tool")
        return {"customer_id": customer_id, "name": "Alice"}

    # First valid call hits the tool.
    await fetch_customer(customer_id="c1")
    assert calls == ["tool"]

    # Same args: cached, tool not called again.
    await fetch_customer(customer_id="c1")
    assert calls == ["tool"]

    # Invalid args: validation runs and fails before cache lookup.
    with pytest.raises(ToolBoundaryError):
        await fetch_customer(customer_id="bad")
    assert calls == ["tool"]


def test_registry_property() -> None:
    config = load_config_from_string(SAMPLE_YAML)
    registry = config.registry

    assert isinstance(registry, ToolRegistry)
    assert registry.allowed_tools == {"fetch_customer", "search_docs"}


def test_runner_factory() -> None:
    config = load_config_from_string(SAMPLE_YAML)
    runner = config.build_runner()

    assert isinstance(runner, ToolRunner)
    assert runner._max_llm_retries == 2
    assert runner._max_tool_retries == 3
    assert runner._registry is not None
    assert runner._registry.allowed_tools == {"fetch_customer", "search_docs"}


def test_runner_factory_accepts_custom_registry() -> None:
    config = load_config_from_string(SAMPLE_YAML)
    custom = ToolRegistry(allowed=["other"])
    runner = config.build_runner(registry=custom)

    assert runner._registry is custom


def test_history_guard_factory() -> None:
    config = load_config_from_string(SAMPLE_YAML)
    guard = config.build_history_guard()

    assert guard is not None
    assert guard._max_tokens == 100000
    assert guard._max_messages == 1000


def test_message_validator_factory() -> None:
    config = load_config_from_string(SAMPLE_YAML)
    validator = config.build_message_validator()

    assert validator is not None
    assert isinstance(validator, config.build_message_validator().__class__)


def test_disabled_message_validator() -> None:
    yaml_text = """
message_validator:
  enabled: false
"""
    config = load_config_from_string(yaml_text)
    assert config.build_message_validator() is None


def test_wrap_module() -> None:
    config = load_config_from_string(SAMPLE_YAML)

    class FakeModule:
        def fetch_customer(self, customer_id: str) -> dict[str, str]:
            return {"customer_id": customer_id, "name": "Alice"}

        def search_docs(self, query: str) -> list[str]:
            return [query]

        def unrelated(self) -> str:
            return "ok"

    namespace = config.wrap_module(FakeModule())

    # Wrapped function is bound with guards.
    with pytest.raises(ToolBoundaryError):
        namespace.fetch_customer(customer_id="bad")

    assert namespace.search_docs(query="x") == ["x"]
    assert namespace.unrelated() == "ok"


def test_invalid_tool_config_raises() -> None:
    yaml_text = """
tools:
  fetch_customer: "not-a-mapping"
"""
    with pytest.raises(ConfigError):
        load_config_from_string(yaml_text)


def test_invalid_protect_config_raises() -> None:
    yaml_text = """
tools:
  fetch_customer:
    protect: "not-a-mapping"
"""
    with pytest.raises(ConfigError):
        load_config_from_string(yaml_text)


def test_empty_config_is_valid() -> None:
    config = load_config_from_string("")
    assert config.tools == {}
    assert config.registry_allowed == []
    assert config.runner_settings == {}
    assert config.build_history_guard() is None
    assert config.build_message_validator() is None


def test_load_config_from_file(tmp_path: Path) -> None:
    path = tmp_path / "mycelium.yaml"
    path.write_text("""
tools:
  fetch_customer:
    bounded:
      schema:
        customer_id: {type: string, required: true}
""")
    config = load_config(path)
    assert "fetch_customer" in config.tools
    assert config.tools["fetch_customer"].bounded is not None


def test_load_config_missing_file_raises() -> None:
    with pytest.raises(ConfigError):
        load_config("/nonexistent/mycelium.yaml")


def test_state_flush_and_audit_receipt_factories() -> None:
    yaml_text = """
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"

state_flush:
  storage: memory
  flush_on:
    - cancel
    - error

audit_receipt:
  signing_key: test-key
  storage: memory
"""
    config = load_config_from_string(yaml_text)
    state_flush = config.build_state_flush()
    audit = config.build_audit_receipt()

    assert state_flush is not None
    assert audit is not None
    assert audit.agent_id == "payment-agent"


def test_apply_tool_with_audit_receipt_from_yaml() -> None:
    yaml_text = """
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"

action_ledger:
  storage: memory
  tools:
    - send_payment

audit_receipt:
  signing_key: test-key
  storage: memory
  auto: true

tools:
  send_payment:
    side_effect_class: payment
"""
    config = load_config_from_string(yaml_text)

    @config.apply
    def send_payment(amount: float, recipient: str) -> dict[str, str]:
        return {"status": "sent"}

    send_payment(amount=10.0, recipient="acct_1", request_id="pay-1")
    audit = config.build_audit_receipt()
    assert audit is not None
    assert len(audit.storage.list_all()) == 1


def test_global_action_ledger_and_ledger_true() -> None:
    yaml_text = """
action_ledger:
  storage: file
  path: /tmp/ledger.json

tools:
  send_payment:
    ledger: true
"""
    config = load_config_from_string(yaml_text)
    tool = config.tools["send_payment"]
    assert tool.ledger == {"storage": "file", "path": "/tmp/ledger.json"}


def test_registry_auto_from_tools() -> None:
    yaml_text = """
tools:
  fetch_customer:
    bounded:
      schema:
        customer_id: {type: string, required: true}
  search_docs:
    bounded:
      schema:
        query: {type: string, required: true}

registry:
  auto: true
"""
    config = load_config_from_string(yaml_text)
    assert set(config.registry_allowed) == {"fetch_customer", "search_docs"}


def test_instrument_applies_tools_and_tasks() -> None:
    yaml_text = """
action_ledger:
  storage: memory
  tools:
    - send_payment

task_ledger:
  storage: memory
  tasks:
    - process_invoice

tools:
  send_payment: {}

tasks:
  process_invoice:
    ledger: true
    id_from:
      - invoice_id
"""
    config = load_config_from_string(yaml_text)

    class FakeModule:
        def send_payment(self, amount: float, recipient: str) -> dict[str, str]:
            return {"status": "sent"}

        def process_invoice(self, invoice_id: str) -> dict[str, str]:
            return {"invoice_id": invoice_id, "status": "paid"}

    namespace = config.instrument(FakeModule())
    namespace.send_payment(amount=1.0, recipient="a", request_id="x")
    r1 = namespace.process_invoice(invoice_id="inv-1")
    r2 = namespace.process_invoice(invoice_id="inv-1")
    assert r1 == r2


def test_task_id_from_top_level_yaml() -> None:
    yaml_text = """
task_ledger:
  storage: memory

tasks:
  process_invoice:
    ledger: true
    id_from:
      - invoice_id
"""
    config = load_config_from_string(yaml_text)
    task = config.tasks["process_invoice"]
    assert task.ledger is not None
    assert task.ledger.get("id_from") == ["invoice_id"]


def test_prepare_messages_records_active_state_flush() -> None:
    yaml_text = """
state_flush:
  storage: memory

message_validator:
  enabled: true
"""
    config = load_config_from_string(yaml_text)
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]

    with config.run("thread-100") as run:
        prepared = config.prepare_messages(messages)
        assert prepared[0]["role"] == "user"
        assert run.state["messages"] == prepared


def test_config_run_scope_with_state_flush() -> None:
    yaml_text = """
state_flush:
  storage: memory
  flush_on:
    - cancel
"""
    config = load_config_from_string(yaml_text)

    with pytest.raises(asyncio.CancelledError):
        with config.run("thread-99") as run:
            run.record({"streamed": "chunk"})
            raise asyncio.CancelledError()

    state_flush = config.build_state_flush()
    assert state_flush is not None
    snapshot = state_flush.load("thread-99")
    assert snapshot is not None
    assert snapshot.state["streamed"] == "chunk"
