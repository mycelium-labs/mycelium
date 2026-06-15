"""AF-004 proof suite — fixtures grounded in real GitHub issues."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from mycelium import (
    ToolBoundaryError,
    ToolBoundaryExhaustedError,
    ToolRegistry,
    ToolRunner,
    bounded,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "af004"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.mark.parametrize(
    "fixture_name",
    [
        "cline-10737-invalid-tool-args.json",
        "langgraph-6431-invalid-input.json",
    ],
)
@pytest.mark.asyncio
async def test_bounded_blocks_invalid_input_from_real_issues(fixture_name: str) -> None:
    fixture = load_fixture(fixture_name)
    tool_name = fixture["tool_name"]

    async def impl(**kwargs: Any) -> dict:
        return {"ok": True}

    impl.__name__ = tool_name
    tool = bounded(schema=fixture["schema_fields"])(impl)

    with pytest.raises(ToolBoundaryError) as exc:
        await tool(**fixture["bad_kwargs"])

    assert exc.value.violation == fixture["violation"]
    assert exc.value.tool_name == tool_name
    assert exc.value.llm_message

    result = await tool(**fixture["good_kwargs"])
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_bounded_blocks_path_outside_scope_cline_8273() -> None:
    fixture = load_fixture("cline-8273-path-scope.json")

    @bounded(
        schema=fixture["schema_fields"],
        allowed_paths=fixture["allowed_paths"],
        path_param=fixture["path_param"],
    )
    async def delete_file(path: str) -> dict:
        return {"deleted": path}

    with pytest.raises(ToolBoundaryError) as exc:
        await delete_file(**fixture["bad_kwargs"])

    assert exc.value.violation == "scope_path"
    assert exc.value.field == "path"
    assert "/.git" in (exc.value.actual or "")

    result = await delete_file(**fixture["good_kwargs"])
    assert result["deleted"] == fixture["good_kwargs"]["path"]


@pytest.mark.asyncio
async def test_bounded_blocks_wrong_output_shape_langchain_34669() -> None:
    fixture = load_fixture("langchain-34669-output-shape.json")

    @bounded(
        schema=fixture["schema_fields"],
        output_schema=fixture["output_schema_fields"],
    )
    async def mcp_search(query: str) -> Any:
        return fixture["bad_output"]

    with pytest.raises(ToolBoundaryError) as exc:
        await mcp_search(query="rate limits")

    assert exc.value.violation == "output_validation_failed"
    assert exc.value.tool_name == "mcp_search"

    @bounded(
        schema=fixture["schema_fields"],
        output_schema=fixture["output_schema_fields"],
    )
    async def mcp_search_ok(query: str) -> Any:
        return fixture["good_output"]

    result = await mcp_search_ok(query="rate limits")
    assert result == fixture["good_output"]


def test_registry_blocks_tool_not_in_allowlist_langchain_35320() -> None:
    fixture = load_fixture("langchain-35320-allowlist.json")
    registry = ToolRegistry(allowed=fixture["allowed_tools"])

    with pytest.raises(ToolBoundaryError) as exc:
        registry.validate_call(fixture["blocked_tool"])

    assert exc.value.violation == "not_in_allowlist"
    assert fixture["blocked_tool"] in exc.value.llm_message
    for allowed in fixture["allowed_tools"]:
        assert allowed in exc.value.llm_message

    registry.validate_call(fixture["allowed_tools"][0])


@pytest.mark.asyncio
async def test_tool_runner_llm_retry_recovers_cline_8779() -> None:
    fixture = load_fixture("cline-8779-llm-retry-recovery.json")
    attempts: list[dict[str, Any]] = []

    @bounded(schema=fixture["schema_fields"])
    async def replace_in_file(path: str, search: str, replace: str) -> dict:
        return {"path": path, "replaced": True}

    async def invoke_llm(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        attempts.append({"messages": list(messages)})
        return messages

    def parse_tool_kwargs(messages: list[dict[str, Any]], tool_name: str) -> dict[str, Any]:
        assert tool_name == fixture["tool_name"]
        return dict(fixture["corrected_kwargs"])

    runner = ToolRunner(max_llm_retries=1)
    messages: list[dict[str, Any]] = [{"role": "user", "content": "fix the file"}]

    result, final_messages = await runner.run_with_llm_retry(
        replace_in_file,
        messages=messages,
        tool_call_id="call_replace_1",
        kwargs=fixture["initial_kwargs"],
        invoke_llm=invoke_llm,
        parse_tool_kwargs=parse_tool_kwargs,
    )

    assert result == {
        "path": fixture["corrected_kwargs"]["path"],
        "replaced": True,
    }
    assert len(attempts) == 1
    assert any(m.get("role") == "tool" for m in final_messages)
    tool_msgs = [m for m in final_messages if m.get("role") == "tool"]
    assert fixture["tool_name"] in tool_msgs[0]["content"] or "replace" in tool_msgs[0]["content"].lower()


@pytest.mark.asyncio
async def test_tool_runner_exhausts_when_llm_never_corrects() -> None:
    fixture = load_fixture("cline-8779-llm-retry-recovery.json")

    @bounded(schema=fixture["schema_fields"])
    async def replace_in_file(path: str, search: str, replace: str) -> dict:
        return {"replaced": True}

    async def invoke_llm(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return messages

    def parse_tool_kwargs(messages: list[dict[str, Any]], tool_name: str) -> dict[str, Any]:
        return dict(fixture["initial_kwargs"])

    runner = ToolRunner(max_llm_retries=0)

    with pytest.raises(ToolBoundaryExhaustedError) as exc:
        await runner.run_with_llm_retry(
            replace_in_file,
            messages=[{"role": "user", "content": "fix"}],
            tool_call_id="call_1",
            kwargs=fixture["initial_kwargs"],
            invoke_llm=invoke_llm,
            parse_tool_kwargs=parse_tool_kwargs,
        )

    assert exc.value.last_error.violation == "missing_required_field"
