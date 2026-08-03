"""Tests for CompletionContract (AF-007)."""

from __future__ import annotations

import pytest

from mycelium.completion_contract import (
    STATUS_ABANDONED,
    STATUS_FAILED,
    STATUS_SUCCESS,
    CompletionContract,
    CompletionMarkError,
    CompletionRefusedError,
    FileCompletionStorage,
    InMemoryCompletionStorage,
    gate_graph_end,
    wrap_final_message,
)
from mycelium.config import ConfigError, load_config_from_string
from mycelium.transition import TransitionScope, execution_scope


def _scope(run_id: str = "run-1") -> TransitionScope:
    return TransitionScope(thread_id="t1", run_id=run_id, node="end")


def test_refuse_when_required_pending() -> None:
    contract = CompletionContract(required=["send_email", "write_pr"])
    with execution_scope(_scope()):
        with pytest.raises(CompletionRefusedError) as exc:
            contract.complete_run()
        assert exc.value.pending_required == ["send_email", "write_pr"]


def test_allow_when_required_success_failed_or_abandoned() -> None:
    contract = CompletionContract(
        required=["a", "b", "c"],
        optional=["d"],
    )
    with execution_scope(_scope()):
        contract.mark("a", STATUS_SUCCESS)
        contract.mark("b", STATUS_FAILED)
        contract.mark("c", STATUS_ABANDONED, reason="not needed for this tenant")
        result = contract.complete_run()
        assert result is not None
        assert result.verdict == "allow_with_warnings"
        assert result.pending_optional == ["d"]


def test_abandoned_requires_reason() -> None:
    contract = CompletionContract(required=["a"])
    with execution_scope(_scope()):
        with pytest.raises(CompletionMarkError, match="reason"):
            contract.mark("a", STATUS_ABANDONED)
        with pytest.raises(CompletionMarkError, match="reason"):
            contract.mark("a", STATUS_ABANDONED, reason="   ")


def test_optional_pending_warns_and_allows() -> None:
    contract = CompletionContract(required=["a"], optional=["slack"])
    with execution_scope(_scope()):
        contract.mark("a", STATUS_SUCCESS)
        with pytest.warns(UserWarning, match="optional"):
            result = contract.complete_run()
        assert result is not None
        assert result.verdict == "allow_with_warnings"
        assert result.pending_optional == ["slack"]


def test_allow_when_all_marked() -> None:
    contract = CompletionContract(required=["a"], optional=["b"])
    with execution_scope(_scope()):
        contract.mark("a", STATUS_SUCCESS)
        contract.mark("b", STATUS_SUCCESS)
        result = contract.complete_run()
        assert result is not None
        assert result.verdict == "allow"
        assert result.pending_optional == []


def test_unknown_subtask_rejected() -> None:
    contract = CompletionContract(required=["a"])
    with execution_scope(_scope()):
        with pytest.raises(CompletionMarkError, match="unknown"):
            contract.mark("nope", STATUS_SUCCESS)


def test_missing_scope_skips() -> None:
    contract = CompletionContract(required=["a"])
    with pytest.warns(UserWarning, match="skipped"):
        assert contract.complete_run() is None


def test_file_storage_round_trip(tmp_path) -> None:
    path = tmp_path / "completion.json"
    contract = CompletionContract(
        FileCompletionStorage(path),
        required=["a"],
        optional=["b"],
    )
    with execution_scope(_scope("run-file")):
        contract.mark("a", STATUS_SUCCESS)
        with pytest.warns(UserWarning, match="optional"):
            result = contract.complete_run()
        assert result is not None
        assert result.verdict == "allow_with_warnings"

    contract2 = CompletionContract(
        FileCompletionStorage(path),
        required=["a"],
        optional=["b"],
    )
    state = contract2.get_state("run-file")
    assert state is not None
    assert state.marks["a"].status == STATUS_SUCCESS


def test_config_build_and_helpers() -> None:
    cfg = load_config_from_string(
        """
completion:
  storage: memory
  required:
    - id: send_email
  optional:
    - slack
"""
    )
    contract = cfg.build_completion_contract()
    assert contract is not None
    assert contract.required == ["send_email"]
    assert contract.optional == ["slack"]
    with execution_scope(_scope("cfg-run")):
        cfg.mark_completion("send_email", "success")
        result = cfg.complete_run()
        assert result is not None
        assert result.verdict == "allow_with_warnings"


def test_config_overlap_errors() -> None:
    with pytest.raises(ConfigError, match="both required and optional"):
        load_config_from_string(
            """
completion:
  storage: memory
  required: [a]
  optional: [a]
"""
        )


def test_config_empty_errors() -> None:
    with pytest.raises(ConfigError, match="at least one"):
        load_config_from_string(
            """
completion:
  storage: memory
"""
        )


def test_config_file_requires_path() -> None:
    with pytest.raises(ConfigError, match="path"):
        load_config_from_string(
            """
completion:
  storage: file
  required: [a]
"""
        )


def test_wrap_final_message_and_gate_end() -> None:
    contract = CompletionContract(required=["a"])
    calls: list[str] = []

    def emit(msg: str) -> str:
        calls.append(msg)
        return msg

    wrapped = wrap_final_message(contract, emit)
    with execution_scope(_scope("w1")):
        with pytest.raises(CompletionRefusedError):
            wrapped("done")
        assert calls == []
        contract.mark("a", STATUS_SUCCESS)
        assert wrapped("done") == "done"
        assert calls == ["done"]

    contract2 = CompletionContract(
        InMemoryCompletionStorage(), required=["x"]
    )
    with execution_scope(_scope("w2")):
        contract2.mark("x", STATUS_SUCCESS)
        result = gate_graph_end(contract2)
        assert result is not None
        assert result.verdict == "allow"


def test_second_complete_still_refuses_until_marked() -> None:
    contract = CompletionContract(required=["a"])
    with execution_scope(_scope()):
        with pytest.raises(CompletionRefusedError):
            contract.complete_run()
        with pytest.raises(CompletionRefusedError):
            contract.complete_run()
        contract.mark("a", STATUS_SUCCESS)
        assert contract.complete_run() is not None


def test_langgraph_completion_gate_end() -> None:
    from mycelium.integrations.langgraph import completion_gate_end

    contract = CompletionContract(required=["ship"])
    with pytest.raises(CompletionRefusedError):
        completion_gate_end(
            contract,
            config={"configurable": {"thread_id": "t", "run_id": "lg1"}},
        )
    contract.mark("ship", STATUS_SUCCESS, scope_key="lg1")
    result = completion_gate_end(
        contract,
        config={"configurable": {"thread_id": "t", "run_id": "lg1"}},
    )
    assert result is not None
    assert result.verdict == "allow"
