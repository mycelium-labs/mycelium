"""
Tests for HistoryGuard summary keyword fidelity check.
"""

from __future__ import annotations

import pytest

from mycelium import HistoryGuard, HistoryTruncatedError


def test_no_tracked_keywords_skips_check() -> None:
    """With no track_keywords, check_summary_fidelity is a no-op."""
    guard = HistoryGuard()
    messages = [
        {"role": "user", "content": "Hello"},
    ]
    guard.validate(messages)
    guard.check_summary_fidelity(messages)
    assert any(e["event"] == "history_summary_fidelity_ok" for e in guard.audit_log()) is False


def test_keywords_present_after_summary_passes() -> None:
    """Tracked keywords are still present after processing."""
    guard = HistoryGuard(track_keywords=["deadline", "refund"])
    original = [
        {"role": "user", "content": "What's the deadline for a refund?"},
        {"role": "assistant", "content": "The refund deadline is 30 days."},
    ]
    summarized = [
        {"role": "user", "content": "refund deadline question"},
        {"role": "assistant", "content": "30 days for refund deadline"},
    ]
    guard.validate(original)
    guard.check_summary_fidelity(summarized)
    assert any(e["event"] == "history_summary_fidelity_ok" for e in guard.audit_log())


def test_keyword_lost_after_summary_raises() -> None:
    """When a tracked keyword is missing after processing, raises."""
    guard = HistoryGuard(track_keywords=["deadline", "refund", "urgent"])
    original = [
        {"role": "user", "content": "This is urgent — what's the refund deadline?"},
    ]
    summarized = [
        {"role": "user", "content": "refund deadline question"},
    ]
    guard.validate(original)
    with pytest.raises(HistoryTruncatedError) as exc_info:
        guard.check_summary_fidelity(summarized)
    assert "urgent" in str(exc_info.value)
    audit = guard.audit_log()
    assert any(e["event"] == "history_summary_keyword_loss" for e in audit)
    loss_event = [e for e in audit if e["event"] == "history_summary_keyword_loss"][0]
    assert "urgent" in loss_event["lost_keywords"]


def test_all_keywords_lost_raises() -> None:
    """All tracked keywords lost after aggressive summarization."""
    guard = HistoryGuard(track_keywords=["deadline", "refund", "urgent"])
    original = [
        {"role": "user", "content": "Urgent: need refund deadline info"},
    ]
    summarized = [
        {"role": "user", "content": "customer question about policy"},
    ]
    guard.validate(original)
    with pytest.raises(HistoryTruncatedError):
        guard.check_summary_fidelity(summarized)


def test_no_keywords_in_original_skips_check() -> None:
    """If no tracked keywords were present in original, check passes silently."""
    guard = HistoryGuard(track_keywords=["deadline", "refund"])
    original = [
        {"role": "user", "content": "What's the weather?"},
    ]
    summarized = [
        {"role": "user", "content": "weather question"},
    ]
    guard.validate(original)
    guard.check_summary_fidelity(summarized)  # no raise


def test_case_insensitive_matching() -> None:
    """Keyword matching is case-insensitive."""
    guard = HistoryGuard(track_keywords=["DEADLINE", "Refund"])
    original = [
        {"role": "user", "content": "What's the DEADLINE for a REFUND?"},
    ]
    summarized = [
        {"role": "user", "content": "deadline refund question"},
    ]
    guard.validate(original)
    guard.check_summary_fidelity(summarized)
    assert any(e["event"] == "history_summary_fidelity_ok" for e in guard.audit_log())
