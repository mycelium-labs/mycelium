#!/usr/bin/env python3
"""Run AF-006 and AF-004 proof demos with citations to real GitHub issues."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdk"))

from pydantic import BaseModel, Field, create_model  # noqa: E402

from mycelium import (  # noqa: E402
    HistoryGuard,
    HistoryTruncatedError,
    MessageValidationError,
    MessageValidator,
    ToolBoundaryError,
    ToolRegistry,
    ToolRunner,
    bounded,
    protect,
    Session,
)

FIXTURES = Path(__file__).parent / "fixtures"
AF004_FIXTURES = FIXTURES / "af004"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def load_af004(name: str) -> dict:
    return json.loads((AF004_FIXTURES / name).read_text())


def schema_from_fields(fields: dict, *, model_name: str) -> type[BaseModel]:
    model_fields: dict = {}
    for fname, spec in fields.items():
        py_type = int if spec.get("type") == "integer" else str
        if spec.get("required"):
            model_fields[fname] = (py_type, Field(...))
        else:
            model_fields[fname] = (py_type | None, None)
    return create_model(model_name, **model_fields)  # type: ignore[call-overload]


def section(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def cite(fixture: dict) -> None:
    print(f"Source: {fixture['source_url']}")
    print(f"Title:  {fixture['source_title']}")
    print(f"Pattern: {fixture['pattern']}")
    print()


def demo_message_validator_repair(fixture_name: str) -> None:
    fixture = load(fixture_name)
    section(f"MessageValidator repair — {fixture['id']}")
    cite(fixture)

    validator = MessageValidator()
    messages = fixture["messages"]

    try:
        validator.validate(messages)
        print("WITHOUT Mycelium: validate passed (unexpected)")
    except MessageValidationError as exc:
        print(f"WITHOUT Mycelium: validate raises {exc.violation!r} at message {exc.message_index}")
        print(f"  → broken transcript would reach the LLM/API")

    repaired = validator.repair(messages)
    validator.validate(repaired)
    print("WITH Mycelium: repair() + validate() passed")
    if fixture["id"] == "langchain-36984":
        ids = [tc["id"] for tc in repaired[1]["tool_calls"]]
        print(f"  → dropped fc_* partials; kept {ids}")


def demo_message_validator_unfixable(fixture_name: str) -> None:
    fixture = load(fixture_name)
    section(f"MessageValidator flag — {fixture['id']}")
    cite(fixture)

    validator = MessageValidator()
    messages = fixture["messages"]

    try:
        validator.validate(messages)
    except MessageValidationError as exc:
        print(f"WITH Mycelium validate(): caught {exc.violation!r} at message {exc.message_index}")

    repaired = validator.repair(messages)
    try:
        validator.validate(repaired)
        print("WITH Mycelium repair()+validate(): still broken (unfixable orphan)")
    except MessageValidationError as exc:
        print(f"WITH Mycelium repair()+validate(): still raises {exc.violation!r}")
    print("  → agent loop can rebuild history instead of calling the LLM with corrupt context")


async def demo_stale_tool_result() -> None:
    fixture = load("stale-tool-result-ttl.json")
    section(f"@protect TTL refetch — {fixture['id']}")
    cite(fixture)

    db = dict(fixture["initial_db"])
    ttl = fixture["ttl_seconds"]

    @protect(entity_param="customer_id", ttl=ttl)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(db[customer_id])

    async with Session():
        first = await fetch_customer(customer_id="c1")
        print(f"First call: plan={first['plan']!r}")

        db.update(fixture["updated_db"])
        cached = await fetch_customer(customer_id="c1")
        print(f"DB updated but within TTL: plan={cached['plan']!r} (still cached)")

        await asyncio.sleep(ttl + 0.02)
        fresh = await fetch_customer(customer_id="c1")
        print(f"After TTL expires: plan={fresh['plan']!r}, seats={fresh['seats']}")
        print("WITH Mycelium: stale cache expires and refetches real backend state")


def demo_history_drop() -> None:
    fixture = load("history-silent-drop.json")
    section(f"HistoryGuard silent drop — {fixture['id']}")
    cite(fixture)

    guard = HistoryGuard()
    before = fixture["messages_before_trim"]
    after = fixture["messages_after_trim"]

    guard.validate(before)
    print(f"Before trim: {len(before)} messages tracked")

    try:
        guard.check_for_drops(after)
        print("WITHOUT Mycelium: drop would go unnoticed")
    except HistoryTruncatedError as exc:
        print(f"WITH Mycelium: {exc}")
        print(f"  → {len(before) - len(after)} message(s) silently removed from history")


async def demo_bounded_input(fixture_name: str) -> None:
    fixture = load_af004(fixture_name)
    section(f"@bounded input — {fixture['id']}")
    cite(fixture)

    schema = schema_from_fields(fixture["schema_fields"], model_name="Input")
    tool_name = fixture["tool_name"]

    async def impl(**kwargs):
        return {"ok": True}

    impl.__name__ = tool_name
    tool = bounded(schema=schema)(impl)

    try:
        await tool(**fixture["bad_kwargs"])
        print("WITHOUT Mycelium: invalid args would reach the tool")
    except ToolBoundaryError as exc:
        print(f"WITH Mycelium: blocked {exc.violation!r} — tool never ran")
        print(f"  → LLM message: {exc.llm_message[:120]}...")

    await tool(**fixture["good_kwargs"])
    print("WITH Mycelium: valid args pass through")


async def demo_bounded_scope() -> None:
    fixture = load_af004("cline-8273-path-scope.json")
    section(f"@bounded scope — {fixture['id']}")
    cite(fixture)

    schema = schema_from_fields(fixture["schema_fields"], model_name="Input")

    @bounded(
        schema=schema,
        allowed_paths=fixture["allowed_paths"],
        path_param=fixture["path_param"],
    )
    async def delete_file(path: str) -> dict:
        return {"deleted": path}

    try:
        await delete_file(**fixture["bad_kwargs"])
    except ToolBoundaryError as exc:
        print(f"WITH Mycelium: blocked {exc.violation!r} on {exc.field!r}")
        print(f"  → path {exc.actual!r} is outside allowed workspace")

    result = await delete_file(**fixture["good_kwargs"])
    print(f"WITH Mycelium: allowed path deleted: {result['deleted']}")


async def demo_bounded_output() -> None:
    fixture = load_af004("langchain-34669-output-shape.json")
    section(f"@bounded output — {fixture['id']}")
    cite(fixture)

    input_schema = schema_from_fields(fixture["schema_fields"], model_name="Input")
    output_schema = schema_from_fields(fixture["output_schema_fields"], model_name="Output")

    @bounded(schema=input_schema, output_schema=output_schema)
    async def mcp_search(query: str):
        return fixture["bad_output"]

    try:
        await mcp_search(query="rate limits")
    except ToolBoundaryError as exc:
        print(f"WITH Mycelium: blocked {exc.violation!r} after tool returned list")
        print("  → downstream would have crashed on wrong shape")


def demo_allowlist() -> None:
    fixture = load_af004("langchain-35320-allowlist.json")
    section(f"ToolRegistry — {fixture['id']}")
    cite(fixture)

    registry = ToolRegistry(allowed=fixture["allowed_tools"])
    try:
        registry.validate_call(fixture["blocked_tool"])
    except ToolBoundaryError as exc:
        print(f"WITH Mycelium: {exc.violation!r} for {fixture['blocked_tool']!r}")
        print(f"  → agent must use: {', '.join(fixture['allowed_tools'])}")


async def demo_llm_retry() -> None:
    fixture = load_af004("cline-8779-llm-retry-recovery.json")
    section(f"ToolRunner LLM retry — {fixture['id']}")
    cite(fixture)

    schema = schema_from_fields(fixture["schema_fields"], model_name="Input")

    @bounded(schema=schema)
    async def replace_in_file(path: str, search: str, replace: str) -> dict:
        return {"path": path, "replaced": True}

    async def invoke_llm(messages):
        return messages

    def parse_tool_kwargs(messages, tool_name):
        return dict(fixture["corrected_kwargs"])

    runner = ToolRunner(max_llm_retries=1)
    result, messages = await runner.run_with_llm_retry(
        replace_in_file,
        messages=[{"role": "user", "content": "fix file"}],
        tool_call_id="call_1",
        kwargs=fixture["initial_kwargs"],
        invoke_llm=invoke_llm,
        parse_tool_kwargs=parse_tool_kwargs,
    )
    print(f"First call missing 'replace' → tool error appended → LLM retry")
    print(f"WITH Mycelium: recovered and returned {result}")


def main() -> None:
    print("Mycelium proof demo (AF-006 + AF-004)")
    print("Each case cites a real GitHub issue and reproduces its failure class.")

    demo_message_validator_repair("langchain-36984-fc-call-duplicate.json")
    demo_message_validator_repair("langchain-31511-nonzero-index.json")
    demo_message_validator_unfixable("langgraph-7117-orphan-tool-result.json")
    asyncio.run(demo_stale_tool_result())
    demo_history_drop()

    print()
    print("#" * 72)
    print("# AF-004 — tool boundary")
    print("#" * 72)

    asyncio.run(demo_bounded_input("cline-10737-invalid-tool-args.json"))
    asyncio.run(demo_bounded_input("langgraph-6431-invalid-input.json"))
    asyncio.run(demo_bounded_scope())
    asyncio.run(demo_bounded_output())
    demo_allowlist()
    asyncio.run(demo_llm_retry())

    section("Done")
    print("Run tests: pytest proof/test_proof.py proof/test_proof_af004.py -v")


if __name__ == "__main__":
    main()
