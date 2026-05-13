"""
Tests for MessageValidator orphaned tool-result detection.
"""

from __future__ import annotations

import pytest

from mycelium import MessageValidationError, MessageValidator


def test_valid_tool_chain_passes() -> None:
    """A normal assistant tool call + tool result should pass."""
    validator = MessageValidator()
    messages = [
        {"role": "user", "content": "What's the weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Sunny, 72F"},
    ]
    result = validator.validate(messages)
    assert result == messages
    assert any(e["event"] == "validation_ok" for e in validator.audit_log())


def test_orphaned_tool_result_raises() -> None:
    """A tool result with no matching assistant tool_call should raise."""
    validator = MessageValidator()
    messages = [
        {"role": "user", "content": "What's the weather?"},
        {"role": "assistant", "content": "It's nice outside."},
        {"role": "tool", "tool_call_id": "call_1", "content": "Sunny, 72F"},
    ]
    with pytest.raises(MessageValidationError) as exc_info:
        validator.validate(messages)
    assert exc_info.value.violation == "orphaned_tool_result"
    assert "call_1" in str(exc_info.value)
    assert any(e["event"] == "validation_error" for e in validator.audit_log())


def test_orphaned_after_assistant_with_different_id() -> None:
    """Tool result id doesn't match any assistant tool_call id."""
    validator = MessageValidator()
    messages = [
        {"role": "user", "content": "What's the weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_xyz", "content": "Sunny, 72F"},
    ]
    with pytest.raises(MessageValidationError) as exc_info:
        validator.validate(messages)
    assert exc_info.value.violation == "orphaned_tool_result"
    assert "call_xyz" in str(exc_info.value)


def test_multiple_tool_calls_one_orphan() -> None:
    """Two tool results, one matches, one doesn't."""
    validator = MessageValidator()
    messages = [
        {"role": "user", "content": "What's the weather and news?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"},
        {"role": "tool", "tool_call_id": "call_2", "content": "No news"},
    ]
    with pytest.raises(MessageValidationError) as exc_info:
        validator.validate(messages)
    assert exc_info.value.violation == "orphaned_tool_result"
    assert "call_2" in str(exc_info.value)


def test_tool_result_before_assistant_not_orphaned() -> None:
    """Tool result that appears BEFORE its assistant message is also orphaned."""
    validator = MessageValidator()
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
    ]
    with pytest.raises(MessageValidationError) as exc_info:
        validator.validate(messages)
    assert exc_info.value.violation == "orphaned_tool_result"
    assert "call_1" in str(exc_info.value)


def test_no_tool_messages_no_orphan_check() -> None:
    """Messages without any tool results should pass."""
    validator = MessageValidator()
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]
    result = validator.validate(messages)
    assert result == messages


def test_missing_tool_call_id_caught_first() -> None:
    """Missing tool_call_id is caught before orphaned check."""
    validator = MessageValidator()
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "content": "result"},  # missing tool_call_id
    ]
    with pytest.raises(MessageValidationError) as exc_info:
        validator.validate(messages)
    assert exc_info.value.violation == "missing_tool_call_id"


# ---------------------------------------------------------------------------
# Misplaced tool results (after intervening assistant)
# ---------------------------------------------------------------------------


def test_tool_result_between_call_and_response_passes() -> None:
    """Tool result between assistant call and response should pass."""
    validator = MessageValidator()
    messages = [
        {"role": "user", "content": "What's the weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"},
        {"role": "assistant", "content": "It's sunny!"},
    ]
    result = validator.validate(messages)
    assert result == messages


def test_tool_result_after_next_assistant_raises() -> None:
    """Tool result that appears after a subsequent assistant message is flagged."""
    validator = MessageValidator()
    messages = [
        {"role": "user", "content": "What's the weather?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"},
        {"role": "assistant", "content": "It's sunny!"},
        {"role": "tool", "tool_call_id": "call_2", "content": "72F"},  # orphane
    ]
    # call_2 is orphaned + misplaced, but orphaned is caught first
    with pytest.raises(MessageValidationError) as exc_info:
        validator.validate(messages)
    assert exc_info.value.violation == "orphaned_tool_result"


def test_misplaced_tool_result_after_unrelated_assistant() -> None:
    """Tool result from assistant1 appears after assistant2's response."""
    validator = MessageValidator()
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "a", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "Result A"},
        {
            "role": "assistant",
            "content": "Got result A, calling B next.",
            "tool_calls": [
                {"id": "call_2", "type": "function", "function": {"name": "b", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_2", "content": "Result B"},
        {"role": "assistant", "content": "All done."},
        {"role": "tool", "tool_call_id": "call_1", "content": "Late result A"},  # misplaced
    ]
    with pytest.raises(MessageValidationError) as exc_info:
        validator.validate(messages)
    assert exc_info.value.violation == "misplaced_tool_result"


def test_misplaced_caught_after_valid_tool_chain() -> None:
    """After a valid tool chain, a misplaced result is still caught."""
    validator = MessageValidator()
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "x", "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "First result"},
        {"role": "assistant", "content": "OK, moving on."},  # response without tool_calls
        {"role": "tool", "tool_call_id": "call_1", "content": "Duplicate result"},  # misplaced
    ]
    with pytest.raises(MessageValidationError) as exc_info:
        validator.validate(messages)
    assert exc_info.value.violation == "misplaced_tool_result"
