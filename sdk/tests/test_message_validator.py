import pytest

from mycelium import MessageValidationError, MessageValidator


def test_validate_accepts_clean_transcript() -> None:
    messages = [
        {"role": "user", "content": "Look up customer c1"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "type": "function", "index": 0}],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": '{"name": "Alice"}'},
    ]

    result = MessageValidator().validate(messages)

    assert result is messages


def test_validate_raises_on_invalid_role() -> None:
    messages = [{"role": "moderator", "content": "hello"}]

    with pytest.raises(MessageValidationError) as exc:
        MessageValidator().validate(messages)

    assert exc.value.violation == "invalid_role"
    assert exc.value.message_index == 0


def test_validate_raises_on_orphan_tool_result() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "type": "function", "index": 0}],
        },
        {"role": "tool", "tool_call_id": "call_missing", "content": "data"},
    ]

    with pytest.raises(MessageValidationError) as exc:
        MessageValidator().validate(messages)

    assert exc.value.violation == "orphaned_tool_result"


def test_validate_raises_on_duplicate_tool_call_ids() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "index": 0},
                {"id": "call_1", "type": "function", "index": 1},
            ],
        }
    ]

    with pytest.raises(MessageValidationError) as exc:
        MessageValidator().validate(messages)

    assert exc.value.violation == "duplicate_tool_call_ids"


def test_repair_drops_fc_partial_tool_calls() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "fc_partial", "type": "function", "index": 0},
                {"id": "call_final", "type": "function", "index": 0},
            ],
        }
    ]

    repaired = MessageValidator().repair(messages)

    assert messages[0]["tool_calls"][0]["id"] == "fc_partial"
    assert [tc["id"] for tc in repaired[0]["tool_calls"]] == ["call_final"]


def test_repair_deduplicates_tool_call_ids() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "index": 0, "name": "old"},
                {"id": "call_1", "type": "function", "index": 1, "name": "new"},
            ],
        }
    ]

    repaired = MessageValidator().repair(messages)

    assert len(repaired[0]["tool_calls"]) == 1
    assert repaired[0]["tool_calls"][0]["name"] == "new"


def test_repair_renumbers_tool_call_indices() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "index": 1},
                {"id": "call_2", "type": "function", "index": 2},
            ],
        }
    ]

    repaired = MessageValidator().repair(messages)

    assert [tc["index"] for tc in repaired[0]["tool_calls"]] == [0, 1]


def test_repair_strips_parsed_artifact() -> None:
    messages = [{"role": "assistant", "content": "ok", "parsed": {"answer": 42}}]

    repaired = MessageValidator().repair(messages)

    assert "parsed" not in repaired[0]
    assert "parsed" in messages[0]


def test_repair_raises_on_missing_tool_call_id() -> None:
    messages = [{"role": "tool", "content": "result without id"}]

    with pytest.raises(MessageValidationError) as exc:
        MessageValidator().repair(messages)

    assert exc.value.violation == "missing_tool_call_id"
