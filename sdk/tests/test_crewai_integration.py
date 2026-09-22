"""Real CrewAI lifecycle hooks with an offline LLM and an in-process tool."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import pytest

from mycelium import CompletionRefusedError, load_config_from_string
from mycelium.completion_contract import reset_completion_terminal_state
from mycelium.integrations import crewai as adapter
from mycelium.transition import (
    TransitionScope,
    dispatch_scope,
    execution_scope,
    get_active_dispatch_id,
    get_active_execution_scope,
)

METHODS = ["kickoff", "kickoff_async", "akickoff"]


@pytest.fixture
def framework(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Any:
    # Disable export before importing CrewAI, and forbid network connections.
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    monkeypatch.setenv("CREWAI_TRACING_ENABLED", "false")
    monkeypatch.setenv("CREWAI_TELEMETRY_ENABLED", "false")
    monkeypatch.setenv("CREWAI_STORAGE_DIR", str(tmp_path))

    def no_network(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("CrewAI integration tests must not connect to the network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    framework = pytest.importorskip("crewai", reason="requires the optional crewai extra")
    # The adapter patches public entry points. Restore them after every test.
    for name in ("kickoff", "akickoff"):
        monkeypatch.setattr(framework.Crew, name, getattr(framework.Crew, name))
    monkeypatch.setattr(adapter, "_active_options", None)
    reset_completion_terminal_state()
    yield framework
    reset_completion_terminal_state()


def _crew(framework: Any, observations: list[Any], *, fail_llm: bool = False) -> Any:
    from crewai.tools import tool

    class OfflineLLM(framework.BaseLLM):
        def call(self, *args: Any, **kwargs: Any) -> str:
            if fail_llm:
                raise RuntimeError("offline LLM failed")
            return "Thought: capture context.\nAction: capture_context\nAction Input: {}"

        async def acall(self, *args: Any, **kwargs: Any) -> str:
            return self.call(*args, **kwargs)

    @tool
    def capture_context() -> str:
        """Capture the identity supplied by the framework's real tool hooks."""
        observations.append((get_active_execution_scope(), get_active_dispatch_id()))
        return "captured"

    capture_context.result_as_answer = True
    agent = framework.Agent(
        role="Context observer",
        goal="Capture the execution context",
        backstory="A synthetic integration test agent",
        llm=OfflineLLM(model="offline-fixture"),
        tools=[capture_context],
        allow_delegation=False,
        max_iter=2,
        max_retry_limit=0,
    )
    task = framework.Task(
        description="Capture the execution context",
        expected_output="captured",
        agent=agent,
    )
    return framework.Crew(agents=[agent], tasks=[task], tracing=False, cache=False)


def _config(*, completion: bool = False) -> Any:
    checklist = (
        "completion: {storage: memory, required: [{id: capture_context}]}\n"
        if completion else ""
    )
    return load_config_from_string(
        "integrations: {crewai: {enabled: true, run_id_from: request_id}}\n"
        + checklist
    )


def _run(crew: Any, method: str, inputs: dict[str, str]) -> Any:
    result = getattr(crew, method)(inputs=inputs)
    return result if method == "kickoff" else asyncio.run(result)


@pytest.mark.parametrize("method", METHODS)
def test_tool_identity_is_stable_and_caller_context_is_restored(
    framework: Any, method: str,
) -> None:
    _config()
    observations: list[Any] = []
    crew = _crew(framework, observations)
    outer = TransitionScope(thread_id="caller-thread", run_id="caller-run", node="caller")
    with execution_scope(outer), dispatch_scope("caller-dispatch"):
        for run_id in ("request-one", "request-one", "request-two"):
            result = _run(crew, method, {"request_id": run_id})
            assert result.raw.startswith("captured")
            assert get_active_execution_scope() == outer
            assert get_active_dispatch_id() == "caller-dispatch"

    assert len(observations) == 3
    first_scope, first_dispatch = observations[0]
    assert first_scope.run_id == "request-one"
    assert first_scope.thread_id.startswith("crewai:")
    assert first_scope.node.startswith("task:") and ":agent:" in first_scope.node
    assert first_dispatch.startswith("crewai:")
    assert observations[1] == observations[0]
    assert observations[2][0].run_id == "request-two"
    assert observations[2][1] != first_dispatch
    assert get_active_execution_scope() is None
    assert get_active_dispatch_id() is None


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("completed", [False, True])
def test_completion_contract_runs_at_real_crew_terminal(
    framework: Any, method: str, completed: bool,
) -> None:
    from crewai.hooks.dispatch import HookAborted

    config = _config(completion=True)
    contract = config.build_completion_contract()
    assert contract is not None
    if completed:
        contract.mark("capture_context", "success", scope_key="request-terminal")
    observations: list[Any] = []
    crew = _crew(framework, observations)
    if completed:
        assert _run(crew, method, {"request_id": "request-terminal"}).raw.startswith("captured")
    else:
        with pytest.raises(HookAborted) as exc:
            _run(crew, method, {"request_id": "request-terminal"})
        assert isinstance(exc.value.__cause__, CompletionRefusedError)
        assert exc.value.__cause__.pending_required == ["capture_context"]
    assert len(observations) == 1
    assert get_active_execution_scope() is None
    assert get_active_dispatch_id() is None


@pytest.mark.parametrize("method", METHODS)
def test_failed_crew_restores_caller_identity(framework: Any, method: str) -> None:
    _config()
    observations: list[Any] = []
    crew = _crew(framework, observations, fail_llm=True)
    outer = TransitionScope(thread_id="caller-thread", run_id="caller-run", node="caller")
    with execution_scope(outer), dispatch_scope("caller-dispatch"):
        with pytest.raises(RuntimeError, match="offline LLM failed"):
            _run(crew, method, {"request_id": "request-failure"})
        assert get_active_execution_scope() == outer
        assert get_active_dispatch_id() == "caller-dispatch"
    assert observations == []


@pytest.mark.parametrize("method", METHODS)
def test_missing_configured_run_id_does_not_start_crew(framework: Any, method: str) -> None:
    _config()
    observations: list[Any] = []
    crew = _crew(framework, observations, fail_llm=True)
    with pytest.raises(adapter.CrewAIIntegrationError, match="stable run id"):
        _run(crew, method, {})
    assert observations == []
    assert get_active_execution_scope() is None
    assert get_active_dispatch_id() is None
