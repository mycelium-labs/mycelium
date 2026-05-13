"""
ToolSequencer — detection of out-of-order tool call results.

Useful in parallel tool-call scenarios where results may arrive in a
different order than the calls were initiated. The sequencer tracks call
order and flags when results complete out of sequence.

Usage::

    from mycelium import ToolSequencer

    seq = ToolSequencer()

    # Start tracking calls
    id1 = seq.begin("fetch_customer", customer_id="c1")
    id2 = seq.begin("get_orders", customer_id="c1")

    # Results arrive (possibly in different order)
    seq.end(id2, "get_orders")  # OK — first to complete
    seq.end(id1, "fetch_customer")  # FLAGGED — completed after a later call

    for event in seq.audit_log():
        if event["event"] == "tool_result_out_of_order":
            print(f"Out of order: {event}")
"""

from __future__ import annotations

import time
from typing import Any


class ToolSequencer:
    """
    Assigns sequence numbers to tool calls and detects out-of-order results.

    Each call to ``begin()`` returns a monotonically increasing sequence ID.
    When ``end()`` is called with that ID, the sequencer checks whether
    results are completing in call order. If a result completes after a
    later-started call, it is flagged as out-of-order.
    """

    def __init__(self) -> None:
        self._counter = 0
        self._in_flight: dict[int, dict[str, Any]] = {}
        self._completed: set[int] = set()
        self._highest_completed = 0
        self._audit: list[dict[str, Any]] = []

    def begin(self, tool_name: str, **context: Any) -> int:
        """
        Record the start of a tool call.

        Returns a monotonically increasing sequence ID. Pass this ID
        to ``end()`` when the result arrives.

        *context* is any additional metadata (entity IDs, args, etc.)
        to include in audit events.
        """
        self._counter += 1
        seq_id = self._counter
        now = time.monotonic()
        self._in_flight[seq_id] = {
            "tool_name": tool_name,
            "context": context,
            "ts": now,
        }
        self._audit.append(
            {
                "event": "tool_call_started",
                "seq_id": seq_id,
                "tool_name": tool_name,
                "context": context,
                "ts": now,
            }
        )
        return seq_id

    def end(self, seq_id: int, tool_name: str | None = None) -> None:
        """
        Record the completion of a tool call.

        If a later-started call has already completed, emits a
        ``tool_result_out_of_order`` audit event.

        Args:
            seq_id: The sequence ID returned by ``begin()``.
            tool_name: Optional tool name for the audit event.
        """
        now = time.monotonic()
        info = self._in_flight.pop(seq_id, None)
        tool = tool_name or (info["tool_name"] if info else "unknown")

        # Check if a later-started call already completed
        if seq_id < self._highest_completed:
            self._audit.append(
                {
                    "event": "tool_result_out_of_order",
                    "seq_id": seq_id,
                    "tool_name": tool,
                    "context": info["context"] if info else {},
                    "completed_after": self._highest_completed,
                    "ts": now,
                }
            )

        self._completed.add(seq_id)
        if seq_id > self._highest_completed:
            self._highest_completed = seq_id

        self._audit.append(
            {
                "event": "tool_call_ended",
                "seq_id": seq_id,
                "tool_name": tool,
                "ts": now,
            }
        )

    @property
    def in_flight(self) -> int:
        """Number of tool calls currently in-flight (not yet ended)."""
        return len(self._in_flight)

    def audit_log(self) -> list[dict[str, Any]]:
        """Return list of all events since construction."""
        return list(self._audit)

    def has_event(self, event: str) -> bool:
        """Return True if *event* appears in the audit log."""
        return any(e["event"] == event for e in self._audit)
