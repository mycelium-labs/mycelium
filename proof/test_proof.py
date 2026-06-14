"""AF-006 proof suite — fixtures grounded in real GitHub issues."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from mycelium import (
    HistoryGuard,
    HistoryTruncatedError,
    MessageValidationError,
    MessageValidator,
    protect,
    Session,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.mark.parametrize(
    "fixture_name",
    [
        "langchain-36984-fc-call-duplicate.json",
        "langchain-31511-nonzero-index.json",
    ],
)
def test_message_validator_repair_fixes_real_issue_patterns(fixture_name: str) -> None:
    fixture = load_fixture(fixture_name)
    validator = MessageValidator()
    messages = fixture["messages"]

    with pytest.raises(MessageValidationError) as exc:
        validator.validate(messages)
    assert exc.value.violation == fixture["violation"]

    repaired = validator.repair(messages)
    validator.validate(repaired)


@pytest.mark.parametrize(
    "fixture_name",
    ["langgraph-7117-orphan-tool-result.json"],
)
def test_message_validator_flags_unfixable_real_issue_patterns(fixture_name: str) -> None:
    fixture = load_fixture(fixture_name)
    validator = MessageValidator()
    messages = fixture["messages"]

    with pytest.raises(MessageValidationError) as exc:
        validator.validate(messages)
    assert exc.value.violation == fixture["violation"]

    repaired = validator.repair(messages)
    with pytest.raises(MessageValidationError) as exc_after_repair:
        validator.validate(repaired)
    assert exc_after_repair.value.violation == fixture["violation"]


@pytest.mark.asyncio
async def test_protect_refetches_after_backend_update() -> None:
    fixture = load_fixture("stale-tool-result-ttl.json")
    db = dict(fixture["initial_db"])
    ttl = fixture["ttl_seconds"]
    entity_param = fixture["entity_param"]

    @protect(entity_param=entity_param, ttl=ttl)
    async def fetch_customer(customer_id: str) -> dict:
        return dict(db[customer_id])

    async with Session():
        first = await fetch_customer(customer_id="c1")
        assert first["plan"] == "pro"

        db.update(fixture["updated_db"])

        stale = await fetch_customer(customer_id="c1")
        assert stale["plan"] == "pro"

        await asyncio.sleep(ttl + 0.02)
        fresh = await fetch_customer(customer_id="c1")
        assert fresh["plan"] == "enterprise"
        assert fresh["seats"] == 50


def test_history_guard_detects_silent_drop_from_real_pattern() -> None:
    fixture = load_fixture("history-silent-drop.json")
    guard = HistoryGuard()

    guard.validate(fixture["messages_before_trim"])

    with pytest.raises(HistoryTruncatedError, match="silently dropped"):
        guard.check_for_drops(fixture["messages_after_trim"])
