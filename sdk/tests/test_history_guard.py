import pytest

from mycelium import HistoryGuard, HistoryTruncatedError
from mycelium.history_guard import estimate_tokens


def test_validate_accepts_history_under_limits() -> None:
    messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]

    guard = HistoryGuard(max_tokens=1000, max_messages=10)
    result = guard.validate(messages)

    assert result is messages
    assert any(entry["event"] == "history_ok" for entry in guard.audit_log())


def test_validate_raises_on_token_overflow() -> None:
    messages = [{"role": "user", "content": "x" * 400}]

    guard = HistoryGuard(max_tokens=10)

    with pytest.raises(HistoryTruncatedError) as exc:
        guard.validate(messages)

    assert exc.value.estimated_tokens > 10
    assert exc.value.message_count == 1


def test_validate_raises_on_message_count_limit() -> None:
    messages = [{"role": "user", "content": f"msg {index}"} for index in range(5)]

    guard = HistoryGuard(max_messages=3)

    with pytest.raises(HistoryTruncatedError) as exc:
        guard.validate(messages)

    assert exc.value.message_count == 5


def test_validate_raises_on_duplicate_turns() -> None:
    duplicate = {"role": "user", "content": "same question"}
    messages = [duplicate, {"role": "assistant", "content": "answer"}, duplicate]

    guard = HistoryGuard(detect_duplicates=True)

    with pytest.raises(HistoryTruncatedError, match="Duplicate turns"):
        guard.validate(messages)


def test_check_for_drops_detects_silent_removal() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]

    guard = HistoryGuard()
    guard.validate(messages)

    with pytest.raises(HistoryTruncatedError, match="silently dropped"):
        guard.check_for_drops(messages[:2])


def test_check_for_drops_requires_validate_first() -> None:
    guard = HistoryGuard()

    with pytest.raises(RuntimeError, match="before validate"):
        guard.check_for_drops([{"role": "user", "content": "hello"}])


def test_estimate_tokens_counts_message_overhead() -> None:
    messages = [{"role": "user", "content": "abcd"}]

    assert estimate_tokens(messages) >= 5
