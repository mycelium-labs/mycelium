"""
ScratchpadGuard — protection against uncoordinated shared state in multi-agent systems.

Wraps shared dicts with access logging that detects:

  1. **Overwrites** — one agent writes to a key last written by a different agent
     without an explicit handoff.
  2. **Read-before-write** — an agent reads a key that has never been written.
  3. **Cross-agent deletion** — an agent deletes a key created by another agent.

Usage::

    from mycelium import ScratchpadGuard

    guard = ScratchpadGuard()

    # Agent A writes
    shared = guard.wrap({}, name="planner")
    shared["task"] = "design API"

    # Agent B reads — fine
    shared = guard.wrap(shared, name="coder")
    print(shared["task"])

    # Agent B overwrites — FLAGGED (writer "coder" != original writer "planner")
    shared["task"] = "rewrite everything"

    # Inspect
    for event in guard.audit_log():
        print(event)
"""

from __future__ import annotations

import time
from typing import Any


class ScratchpadGuard:
    """
    Wraps shared dicts to detect uncoordinated multi-agent state access.

    Use ``wrap(shared_dict, name=agent_name)`` to create a monitored proxy.
    All reads, writes, and deletes are logged in ``audit_log()``.
    """

    def __init__(self) -> None:
        self._writers: dict[str, str] = {}  # key -> writer name
        self._audit: list[dict[str, Any]] = []

    def wrap(self, target: dict, name: str) -> dict:
        """
        Wrap *target* dict in a monitored proxy that logs access under *name*.

        The proxy preserves all dict methods (get, pop, update, etc.) while
        intercepting __getitem__, __setitem__, and __delitem__ for logging.
        """
        writers = self._writers
        audit = self._audit

        class _ScratchpadProxy(dict):
            def __init__(self) -> None:
                super().__init__()
                for k, v in target.items():
                    dict.__setitem__(self, k, v)
                    if k not in writers:
                        writers[k] = name

            def __setitem__(self, key: str, value: Any) -> None:
                now = time.monotonic()
                prev_writer = writers.get(key)
                if prev_writer is not None and prev_writer != name:
                    audit.append(
                        {
                            "event": "scratchpad_overwrite",
                            "key": key,
                            "writer": name,
                            "previous_writer": prev_writer,
                            "ts": now,
                        }
                    )
                writers[key] = name
                audit.append(
                    {
                        "event": "scratchpad_write",
                        "key": key,
                        "writer": name,
                        "ts": now,
                    }
                )
                dict.__setitem__(self, key, value)

            def __getitem__(self, key: str) -> Any:
                now = time.monotonic()
                if key not in writers:
                    audit.append(
                        {
                            "event": "scratchpad_read_before_write",
                            "key": key,
                            "reader": name,
                            "ts": now,
                        }
                    )
                audit.append(
                    {
                        "event": "scratchpad_read",
                        "key": key,
                        "reader": name,
                        "ts": now,
                    }
                )
                return dict.__getitem__(self, key)

            def __delitem__(self, key: str) -> None:
                now = time.monotonic()
                prev_writer = writers.get(key)
                audit.append(
                    {
                        "event": "scratchpad_delete",
                        "key": key,
                        "deleter": name,
                        "previous_writer": prev_writer,
                        "ts": now,
                    }
                )
                writers.pop(key, None)
                dict.__delitem__(self, key)

        return _ScratchpadProxy()

    def audit_log(self) -> list[dict[str, Any]]:
        """Return list of all access events since construction."""
        return list(self._audit)

    def has_event(self, event: str) -> bool:
        """Return True if *event* appears in the audit log."""
        return any(e["event"] == event for e in self._audit)
