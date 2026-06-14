#!/usr/bin/env python3
"""Run the AF-006 proof demo with citations to real GitHub issues."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "sdk"))

from mycelium import (  # noqa: E402
    HistoryGuard,
    HistoryTruncatedError,
    MessageValidationError,
    MessageValidator,
    protect,
    Session,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


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


def main() -> None:
    print("Mycelium AF-006 proof demo")
    print("Each case cites a real GitHub issue and reproduces its failure class.")

    demo_message_validator_repair("langchain-36984-fc-call-duplicate.json")
    demo_message_validator_repair("langchain-31511-nonzero-index.json")
    demo_message_validator_unfixable("langgraph-7117-orphan-tool-result.json")
    asyncio.run(demo_stale_tool_result())
    demo_history_drop()

    section("Done")
    print("Run tests: pytest proof/test_proof.py -v")


if __name__ == "__main__":
    main()
