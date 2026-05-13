"""
Tests for ToolSequencer — out-of-order tool result detection.
"""

from __future__ import annotations

from mycelium import ToolSequencer


def test_in_order_results_no_flag() -> None:
    """Results arriving in call order should not trigger out-of-order."""
    seq = ToolSequencer()

    id1 = seq.begin("fetch_customer", customer_id="c1")
    id2 = seq.begin("get_orders", customer_id="c1")

    seq.end(id1, "fetch_customer")
    seq.end(id2, "get_orders")

    assert not seq.has_event("tool_result_out_of_order")


def test_out_of_order_results_flagged() -> None:
    """Result from later call completing before earlier call is flagged."""
    seq = ToolSequencer()

    id1 = seq.begin("fetch_customer", customer_id="c1")
    id2 = seq.begin("get_orders", customer_id="c1")

    # id2 completes first
    seq.end(id2, "get_orders")
    # id1 completes later — flagged
    seq.end(id1, "fetch_customer")

    assert seq.has_event("tool_result_out_of_order")
    events = [e for e in seq.audit_log() if e["event"] == "tool_result_out_of_order"]
    assert len(events) == 1
    assert events[0]["seq_id"] == 1
    assert events[0]["tool_name"] == "fetch_customer"
    assert events[0]["completed_after"] == 2


def test_single_call_no_flag() -> None:
    """Single tool call should never trigger out-of-order."""
    seq = ToolSequencer()

    id1 = seq.begin("fetch_customer", customer_id="c1")
    seq.end(id1, "fetch_customer")

    assert not seq.has_event("tool_result_out_of_order")


def test_three_calls_middle_out_of_order() -> None:
    """Call 1 and 3 complete, then 2 completes — flags call 2."""
    seq = ToolSequencer()

    id1 = seq.begin("a")
    id2 = seq.begin("b")
    id3 = seq.begin("c")

    seq.end(id1, "a")  # OK — seq 1
    seq.end(id3, "c")  # OK — seq 3 (highest_completed=3)
    seq.end(id2, "b")  # FLAGGED — seq 2 < highest_completed (3)

    assert seq.has_event("tool_result_out_of_order")
    events = [e for e in seq.audit_log() if e["event"] == "tool_result_out_of_order"]
    assert len(events) == 1
    assert events[0]["tool_name"] == "b"


def test_in_flight_count() -> None:
    """in_flight property tracks active calls."""
    seq = ToolSequencer()

    assert seq.in_flight == 0

    id1 = seq.begin("a")
    assert seq.in_flight == 1

    id2 = seq.begin("b")
    assert seq.in_flight == 2

    seq.end(id1, "a")
    assert seq.in_flight == 1

    seq.end(id2, "b")
    assert seq.in_flight == 0


def test_audit_log_contains_all_events() -> None:
    """Audit log contains start and end events."""
    seq = ToolSequencer()

    id1 = seq.begin("fetch", key="val")
    seq.end(id1, "fetch")

    log = seq.audit_log()
    assert any(e["event"] == "tool_call_started" for e in log)
    assert any(e["event"] == "tool_call_ended" for e in log)
