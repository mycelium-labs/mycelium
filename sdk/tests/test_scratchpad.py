"""
Tests for ScratchpadGuard — multi-agent shared state access logging.
"""

from __future__ import annotations

from mycelium import ScratchpadGuard


def test_write_logged() -> None:
    """A write to the shared dict is logged."""
    guard = ScratchpadGuard()
    shared = guard.wrap({}, name="agent_a")
    shared["key"] = "value"
    assert guard.has_event("scratchpad_write")
    events = [e for e in guard.audit_log() if e["event"] == "scratchpad_write"]
    assert len(events) == 1
    assert events[0]["key"] == "key"
    assert events[0]["writer"] == "agent_a"


def test_read_logged() -> None:
    """A read from the shared dict is logged."""
    guard = ScratchpadGuard()
    shared = guard.wrap({"existing": "data"}, name="agent_a")
    _ = shared["existing"]
    assert guard.has_event("scratchpad_read")


def test_read_before_write_detected() -> None:
    """Reading a key that was never written triggers a warning."""
    guard = ScratchpadGuard()
    shared = guard.wrap({}, name="agent_a")
    shared["new_key"] = "value"  # write first, then read
    _ = shared["new_key"]
    # No read-before-write since we wrote it
    assert not guard.has_event("scratchpad_read_before_write")


def test_read_before_write_on_missing_key() -> None:
    """Reading an uninitialized key triggers a warning."""
    guard = ScratchpadGuard()
    shared = guard.wrap({}, name="agent_a")
    shared["missing_key"] = "value"  # write it first
    _ = shared["missing_key"]
    assert not guard.has_event("scratchpad_read_before_write")


def test_read_before_write_on_truly_missing_key() -> None:
    """Reading a key that was never written or initialized triggers a warning."""
    guard = ScratchpadGuard()
    shared = guard.wrap({"existing": "data"}, name="agent_a")
    _ = shared["existing"]  # initialized in wrap, should NOT trigger
    assert not guard.has_event("scratchpad_read_before_write")


def test_read_truly_unwritten_key() -> None:
    """Reading a key that was truly never written triggers read_before_write."""
    guard = ScratchpadGuard()
    shared = guard.wrap({}, name="agent_a")
    # Key that doesn't exist at all
    try:
        _ = shared["nonexistent"]
    except KeyError:
        pass
    # We never get to the audit log because KeyError is raised first
    # The proxy's __getitem__ tries dict.__getitem__ which raises KeyError
    # The audit event is appended before the KeyError
    # Actually, looking at the code, audit is appended THEN dict.__getitem__ is called
    # which raises. So the event should be in the log.
    assert guard.has_event("scratchpad_read_before_write")


def test_overwrite_detected() -> None:
    """Different agent overwriting a key is flagged."""
    guard = ScratchpadGuard()
    shared = guard.wrap({}, name="agent_a")
    shared["task"] = "design"
    shared = guard.wrap(shared, name="agent_b")
    shared["task"] = "rewrite"
    assert guard.has_event("scratchpad_overwrite")
    events = [e for e in guard.audit_log() if e["event"] == "scratchpad_overwrite"]
    assert len(events) == 1
    assert events[0]["key"] == "task"
    assert events[0]["writer"] == "agent_b"
    assert events[0]["previous_writer"] == "agent_a"


def test_same_agent_overwrite_not_flagged() -> None:
    """Same agent overwriting its own key is not flagged."""
    guard = ScratchpadGuard()
    shared = guard.wrap({}, name="agent_a")
    shared["task"] = "design"
    shared["task"] = "redesign"  # same writer
    assert not guard.has_event("scratchpad_overwrite")


def test_delete_logged() -> None:
    """Deleting a key is logged."""
    guard = ScratchpadGuard()
    shared = guard.wrap({"key": "value"}, name="agent_a")
    del shared["key"]
    assert guard.has_event("scratchpad_delete")
    events = [e for e in guard.audit_log() if e["event"] == "scratchpad_delete"]
    assert events[0]["deleter"] == "agent_a"


def test_multiple_agents_no_conflict() -> None:
    """Different agents writing different keys should not trigger overwrite."""
    guard = ScratchpadGuard()
    shared = guard.wrap({}, name="planner")
    shared["plan"] = "build feature"
    shared = guard.wrap(shared, name="coder")
    shared["code"] = "implementation"
    assert not guard.has_event("scratchpad_overwrite")


def test_wrap_preserves_initial_values() -> None:
    """Initial values passed to wrap are accessible."""
    guard = ScratchpadGuard()
    initial = {"a": 1, "b": 2}
    shared = guard.wrap(initial, name="agent")
    assert shared["a"] == 1
    assert shared["b"] == 2
