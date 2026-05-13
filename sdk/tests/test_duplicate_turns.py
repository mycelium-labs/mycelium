"""
Tests for HistoryGuard duplicate turn detection.
"""

from __future__ import annotations

import pytest

from mycelium import HistoryGuard, HistoryTruncatedError


def test_no_duplicates_passes() -> None:
    """All-unique messages should pass without error."""
    guard = HistoryGuard()
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "What's the weather?"},
    ]
    result = guard.validate(messages)
    assert result == messages
    assert any(e["event"] == "history_ok" for e in guard.audit_log())


def test_duplicate_turn_raises() -> None:
    """A repeated assistant message should raise HistoryTruncatedError."""
    guard = HistoryGuard()
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "assistant", "content": "Hi there!"},  # duplicate
    ]
    with pytest.raises(HistoryTruncatedError) as exc_info:
        guard.validate(messages)
    assert "Duplicate turns detected" in str(exc_info.value)
    assert exc_info.value.message_count == 4
    audit = guard.audit_log()
    assert any(e["event"] == "history_duplicate_turns" for e in audit)
    dup_event = [e for e in audit if e["event"] == "history_duplicate_turns"][0]
    assert dup_event["duplicate_count"] == 1
    assert 3 in dup_event["duplicate_indices"]


def test_multiple_duplicates_detected() -> None:
    """Multiple duplicate messages should all be reported."""
    guard = HistoryGuard()
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "user", "content": "Hello"},  # duplicate of index 0
        {"role": "assistant", "content": "Hi there!"},  # duplicate of index 1
    ]
    with pytest.raises(HistoryTruncatedError):
        guard.validate(messages)
    audit = guard.audit_log()
    dup_event = [e for e in audit if e["event"] == "history_duplicate_turns"][0]
    assert dup_event["duplicate_count"] == 2
    assert 2 in dup_event["duplicate_indices"]
    assert 3 in dup_event["duplicate_indices"]


def test_duplicate_detection_disabled() -> None:
    """With detect_duplicates=False, duplicates are allowed."""
    guard = HistoryGuard(detect_duplicates=False)
    messages = [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
        {"role": "assistant", "content": "Hi there!"},  # duplicate
    ]
    result = guard.validate(messages)
    assert result == messages
    assert not any(e["event"] == "history_duplicate_turns" for e in guard.audit_log())


def test_duplicate_with_langchain_messages() -> None:
    """Duplicate detection works with object-style messages too."""

    class FakeMessage:
        def __init__(self, role: str, content: str):
            self.type = role
            self.content = content

    guard = HistoryGuard()
    messages = [
        FakeMessage("user", "Hello"),
        FakeMessage("assistant", "Hi!"),
        FakeMessage("user", "Hello"),  # duplicate
    ]
    with pytest.raises(HistoryTruncatedError):
        guard.validate(messages)
    assert any(e["event"] == "history_duplicate_turns" for e in guard.audit_log())


def test_empty_messages_no_duplicates() -> None:
    """Empty message list should pass."""
    guard = HistoryGuard()
    result = guard.validate([])
    assert result == []
    assert any(e["event"] == "history_ok" for e in guard.audit_log())
