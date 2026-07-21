"""LangGraph ToolRuntime identity propagation."""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode, ToolRuntime

from mycelium import ConfigError, get_ledger, load_config_from_string


def _config(*, integration: str = "{enabled: true}"):
    return load_config_from_string(
        f"""
integrations:
  langgraph: {integration}
transition:
  agent_id: test-agent
  policy_version: "1"
action_ledger:
  storage: memory
  tools: [send_payment]
tools:
  send_payment:
    side_effect_class: non_idempotent_mutate
"""
    )


def _tool_message(amount: float = 10.0, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "send_payment",
                "args": {"amount": amount},
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _graph_for(tool):
    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([tool]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    return builder.compile()


def test_config_parses_langgraph_integration() -> None:
    config = _config()
    assert config.langgraph_enabled
    assert load_config_from_string("integrations: {langgraph: true}").langgraph_enabled
    assert not load_config_from_string(
        "integrations: {langgraph: {enabled: false}}"
    ).langgraph_enabled


@pytest.mark.parametrize(
    ("yaml_text", "message"),
    [
        ("integrations: langgraph", "'integrations' must be a mapping"),
        (
            "integrations: {langgraph: 'yes'}",
            "'integrations.langgraph' must be a mapping or boolean",
        ),
        (
            "integrations: {langgraph: {enabled: 'yes'}}",
            "'integrations.langgraph.enabled' must be a boolean",
        ),
        (
            "integrations: {crewai: true}",
            "unsupported integration(s): crewai",
        ),
    ],
)
def test_config_rejects_invalid_integrations(yaml_text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config_from_string(yaml_text)


def test_apply_exposes_hidden_tool_runtime_to_langgraph() -> None:
    config = _config()

    @config.apply
    def send_payment(amount: float) -> dict[str, float]:
        """Send one payment."""
        return {"amount": amount}

    signature = inspect.signature(send_payment)
    assert signature.parameters["runtime"].annotation is ToolRuntime

    node = ToolNode([send_payment])
    injected = node._injected_args["send_payment"]
    assert injected.runtime == "runtime"
    assert "runtime" in injected.all_injected_keys


def test_langgraph_redispatch_uses_same_transition_without_manual_ids() -> None:
    config = _config()
    calls: list[float] = []

    @config.apply
    def send_payment(amount: float) -> dict[str, float]:
        """Send one payment."""
        calls.append(amount)
        return {"amount": amount}

    graph = _graph_for(send_payment)
    runtime_config = {
        "configurable": {"thread_id": "thread_1"},
        "run_id": "run_1",
    }

    first = graph.invoke({"messages": [_tool_message()]}, runtime_config)
    second = graph.invoke({"messages": [_tool_message()]}, runtime_config)

    assert calls == [10.0]
    assert first["messages"][-1].content == '{"amount": 10.0}'
    assert second["messages"][-1].content == '{"amount": 10.0}'

    ledger = get_ledger(send_payment)
    assert ledger is not None
    entries = ledger._storage.list_all()
    assert len(entries) == 1
    assert entries[0].tool == "send_payment"


def test_direct_call_without_langgraph_runtime_still_works() -> None:
    config = _config()

    @config.apply
    def send_payment(amount: float) -> dict[str, float]:
        """Send one payment."""
        return {"amount": amount}

    assert send_payment(5.0) == {"amount": 5.0}


def test_explicit_tool_call_id_overrides_runtime_identity() -> None:
    config = _config()
    calls: list[float] = []

    @config.apply
    def send_payment(amount: float) -> dict[str, float]:
        """Send one payment."""
        calls.append(amount)
        return {"amount": amount}

    class Runtime:
        tool_call_id = "runtime-call"
        execution_info = None
        config = {
            "configurable": {"thread_id": "thread_1"},
            "run_id": "run_1",
            "metadata": {"langgraph_node": "tools"},
        }

    send_payment(3.0, tool_call_id="explicit-call", runtime=Runtime())
    send_payment(3.0, tool_call_id="explicit-call", runtime=Runtime())
    assert calls == [3.0]


def test_langgraph_thread_run_and_node_scope_transition_identity() -> None:
    config = _config()
    calls: list[float] = []

    @config.apply
    def send_payment(amount: float) -> dict[str, float]:
        """Send one payment."""
        calls.append(amount)
        return {"amount": amount}

    def runtime(thread_id: str, node: str):
        return SimpleNamespace(
            tool_call_id="same-call",
            execution_info=SimpleNamespace(thread_id=thread_id, run_id="run_1"),
            config={"metadata": {"langgraph_node": node}},
        )

    send_payment(4.0, runtime=runtime("thread_1", "tools_a"))
    send_payment(4.0, runtime=runtime("thread_2", "tools_a"))
    send_payment(4.0, runtime=runtime("thread_2", "tools_b"))

    assert calls == [4.0, 4.0, 4.0]
    ledger = get_ledger(send_payment)
    assert ledger is not None
    assert len(ledger._storage.list_all()) == 3


def test_existing_runtime_parameter_is_rejected() -> None:
    config = _config()

    with pytest.raises(ConfigError, match="already declares a 'runtime' parameter"):

        @config.apply
        def send_payment(amount: float, runtime: object) -> dict[str, float]:
            """Send one payment."""
            del runtime
            return {"amount": amount}
