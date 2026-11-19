"""CrewAI integration: stable run identity and a completion terminal.

Shows the two things a CrewAI host usually needs from Mycelium:

1. ``integrations.crewai`` with ``run_id_from`` binds every tool call in a crew
   kickoff to the stable business identity the host passes in ``inputs``.
2. A configured completion contract is enforced at the real CrewAI terminal, so
   a crew that never recorded its required tool evidence fails closed.

The tool is a credential-free fake. The LLM is a local scripted stub, so this
walkthrough makes no provider or model calls.

Install the optional extra first (it is not a core dependency)::

    python -m pip install -e './sdk[crewai]'

Run from the sdk directory::

    python examples/crewai_fake_order_lookup/run.py
"""

from __future__ import annotations

from typing import Any

from mycelium import (
    CompletionRefusedError,
    load_config_from_string,
    reset_completion_terminal_state,
)
from mycelium.completion_contract import get_active_completion_contract
from mycelium.integrations.crewai import CrewAIIntegrationError
from mycelium.transition import (
    get_active_dispatch_id,
    get_active_execution_scope,
)

RUN_ID_CONFIG = """
integrations:
  crewai:
    enabled: true
    run_id_from: request_id
"""

COMPLETION_CONFIG = (
    RUN_ID_CONFIG
    + """
completion:
  storage: memory
  required:
    - id: lookup_order
"""
)


def _available() -> bool:
    try:
        import crewai  # noqa: F401
        from crewai.hooks.dispatch import HookAborted  # noqa: F401
    except ImportError:
        return False
    return True


def main() -> None:
    print("=== CrewAI integration: identity and completion terminal ===\n")

    if not _available():
        print(
            "The optional CrewAI extra is not installed, so this walkthrough\n"
            "cannot start a crew. Install it and run again:\n\n"
            "    python -m pip install -e './sdk[crewai]'\n"
        )
        print("crewai integration walkthrough complete: status=skipped_missing_extra")
        return

    import crewai
    from crewai import Agent, Crew, Task
    from crewai.hooks.dispatch import HookAborted
    from crewai.tools import tool

    calls: list[tuple[str | None, str | None, str]] = []

    def make_crew(*, record_evidence: bool) -> Any:
        @tool  # type: ignore[misc]
        def lookup_order(order_id: str) -> str:
            """Look up a fake order in local memory."""
            scope = get_active_execution_scope()
            # Mycelium fills these from the crew/run/task/agent metadata.
            calls.append(
                (
                    scope.run_id if scope is not None else None,
                    get_active_dispatch_id(),
                    order_id,
                )
            )
            if record_evidence:
                contract = get_active_completion_contract()
                if contract is not None:
                    contract.mark(
                        "lookup_order",
                        "success",
                        scope_key=scope.run_id if scope is not None else None,
                    )
            return f"order {order_id}: shipped"

        # Keep the scripted stub's answer instead of forcing a final answer.
        lookup_order.result_as_answer = True  # type: ignore[attr-defined]

        class OfflineLLM(crewai.BaseLLM):
            """Deterministic local model. Never calls a provider."""

            def call(self, *args: Any, **kwargs: Any) -> str:
                return (
                    "Thought: look up the order.\n"
                    "Action: lookup_order\n"
                    'Action Input: {"order_id": "ORD-1"}'
                )

            async def acall(self, *args: Any, **kwargs: Any) -> str:
                return self.call(*args, **kwargs)

        agent = Agent(
            role="Order support",
            goal="Answer order status questions",
            backstory="A synthetic agent used by this walkthrough",
            llm=OfflineLLM(model="offline-example"),
            tools=[lookup_order],
            allow_delegation=False,
            max_iter=2,
            max_retry_limit=0,
        )
        task = Task(
            description="Report the status of order ORD-1",
            expected_output="The order status",
            agent=agent,
        )
        return Crew(agents=[agent], tasks=[task], tracing=False, cache=False)

    # 1) run_id_from binds tool calls to the host-supplied identity.
    reset_completion_terminal_state()
    load_config_from_string(RUN_ID_CONFIG)
    calls.clear()
    result = make_crew(record_evidence=False).kickoff(inputs={"request_id": "order-ORD-1"})

    print("1) Kickoff with a stable host run id")
    print(f"   crew answered: {result.raw}")
    print(f"   tool calls: {len(calls)}")
    if not calls:
        raise RuntimeError("the crew did not call the tool")

    run_ids = {run_id for run_id, _dispatch, _order in calls}
    dispatch_ids = {dispatch for _run_id, dispatch, _order in calls}
    print(f"   run_id seen by the tool: {sorted(str(item) for item in run_ids)}")
    assert run_ids == {"order-ORD-1"}, "run_id_from must bind the kickoff identity"
    assert None not in dispatch_ids, "the crew tool must run inside a dispatch scope"

    # 2) A missing kickoff input fails before the crew starts.
    print("\n2) Kickoff without the configured run id field")
    calls.clear()
    try:
        make_crew(record_evidence=False).kickoff(inputs={"order_id": "ORD-1"})
    except CrewAIIntegrationError as exc:
        print(f"   REFUSED before any tool call: {type(exc).__name__}")
    assert not calls, "a missing stable id must not reach the tool"

    # 3) A crew that skipped required evidence is refused at the real terminal.
    print("\n3) Completion contract refuses a crew with missing evidence")
    reset_completion_terminal_state()
    load_config_from_string(COMPLETION_CONFIG)
    try:
        make_crew(record_evidence=False).kickoff(inputs={"request_id": "order-ORD-2"})
    except HookAborted as exc:
        cause = exc.__cause__
        if not isinstance(cause, CompletionRefusedError):
            raise
        print(f"   REFUSED terminal: pending_required={cause.pending_required}")
    else:
        raise RuntimeError("the completion contract did not run at the crew terminal")

    # 4) The same crew completes when the tool records its evidence.
    print("\n4) Completion contract accepts recorded evidence")
    reset_completion_terminal_state()
    load_config_from_string(COMPLETION_CONFIG)
    calls.clear()
    accepted = make_crew(record_evidence=True).kickoff(inputs={"request_id": "order-ORD-3"})
    print(f"   crew answered: {accepted.raw}")
    print(f"   tool calls: {len(calls)}")

    reset_completion_terminal_state()
    print(
        "\nThe host supplies a stable run id per real-world request, so a retry\n"
        "keeps the same transition identity. Production consequential tools still\n"
        "need their own host-owned request_id.\n"
    )
    print(
        "crewai integration walkthrough complete: status=verified "
        f"run_id_bound=True completion_refused=True completion_accepted=True "
        f"tool_calls={len(calls)}"
    )


if __name__ == "__main__":
    main()
