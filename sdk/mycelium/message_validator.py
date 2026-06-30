"""
MessageValidator: catches broken transcripts before LLM calls.

Detects orphan tool results, duplicate IDs, bad roles, and related
serialization bugs. repair() fixes what it can; validate() raises on any issue.
"""

from __future__ import annotations

import time
from copy import deepcopy
from typing import Any


class MessageValidationError(Exception):
    """Raised when message history has structural errors."""

    def __init__(self, reason: str, violation: str, message_index: int = -1) -> None:
        super().__init__(reason)
        self.violation = violation
        self.message_index = message_index


_VALID_ROLES = frozenset({"system", "user", "assistant", "tool", "function"})


def _get_tool_calls(message: dict[str, Any]) -> list[Any]:
    return message.get("tool_calls") or []


def _tool_call_id(tool_call: Any) -> str | None:
    if isinstance(tool_call, dict):
        return tool_call.get("id")
    return None


def _tool_call_index(tool_call: Any) -> int | None:
    if isinstance(tool_call, dict):
        index = tool_call.get("index")
        if index is not None:
            return int(index)
    return None


def _is_fc_partial(tool_call: Any) -> bool:
    tool_id = _tool_call_id(tool_call)
    return isinstance(tool_id, str) and tool_id.startswith("fc_")


def _is_call_final(tool_call: Any) -> bool:
    tool_id = _tool_call_id(tool_call)
    return isinstance(tool_id, str) and tool_id.startswith("call_")


class MessageValidator:
    """
    Validates and optionally repairs message list structure before LLM calls.

    validate(messages): raises MessageValidationError on the first violation.
    repair(messages)  : auto-fixes repairable issues; raises on unrecoverable ones.
    """

    def __init__(
        self,
        *,
        strict_roles: bool = True,
        check_indices: bool = True,
        check_parsed: bool = True,
    ) -> None:
        self._strict_roles = strict_roles
        self._check_indices = check_indices
        self._check_parsed = check_parsed
        self._audit: list[dict[str, Any]] = []

    def validate(self, messages: list[Any]) -> list[Any]:
        """Validate message list structure. Returns messages unchanged."""
        now = time.monotonic()
        self._audit.append(
            {"event": "validation_started", "message_count": len(messages), "ts": now}
        )

        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue

            role = message.get("role", "")

            if self._strict_roles and role not in _VALID_ROLES:
                self._record_violation("invalid_role", index)
                raise MessageValidationError(
                    f"Message {index} has invalid role {role!r}. "
                    f"Expected one of {sorted(_VALID_ROLES)}.",
                    violation="invalid_role",
                    message_index=index,
                )

            tool_calls = _get_tool_calls(message)

            if tool_calls:
                has_fc = any(_is_fc_partial(tool_call) for tool_call in tool_calls)
                has_call = any(_is_call_final(tool_call) for tool_call in tool_calls)
                if has_fc and has_call:
                    self._record_violation("duplicate_tool_call_blocks", index)
                    raise MessageValidationError(
                        f"Message {index} contains both partial fc_* and final call_* "
                        f"tool-call blocks.",
                        violation="duplicate_tool_call_blocks",
                        message_index=index,
                    )

                ids = [
                    tool_id
                    for tool_call in tool_calls
                    if (tool_id := _tool_call_id(tool_call))
                ]
                if len(ids) != len(set(ids)):
                    self._record_violation("duplicate_tool_call_ids", index)
                    raise MessageValidationError(
                        f"Message {index} has duplicate tool_call ids: {ids}.",
                        violation="duplicate_tool_call_ids",
                        message_index=index,
                    )

                if self._check_indices:
                    indices = [
                        tool_index
                        for tool_call in tool_calls
                        if (tool_index := _tool_call_index(tool_call)) is not None
                    ]
                    if indices and indices[0] != 0:
                        self._record_violation("nonzero_tool_call_index", index)
                        raise MessageValidationError(
                            f"Message {index} tool_calls start at index {indices[0]}, expected 0.",
                            violation="nonzero_tool_call_index",
                            message_index=index,
                        )

            if role == "tool" and not message.get("tool_call_id"):
                self._record_violation("missing_tool_call_id", index)
                raise MessageValidationError(
                    f"Message {index} has role='tool' but no tool_call_id.",
                    violation="missing_tool_call_id",
                    message_index=index,
                )

            if self._check_parsed and "parsed" in message:
                self._record_violation("parsed_artifact", index)
                raise MessageValidationError(
                    f"Message {index} contains a 'parsed' structured-output artifact.",
                    violation="parsed_artifact",
                    message_index=index,
                )

        has_assistant = any(
            isinstance(message, dict) and message.get("role") == "assistant"
            for message in messages
        )
        known_tool_call_ids: set[str] = set()
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                continue

            role = message.get("role", "")
            if role == "assistant":
                for tool_call in _get_tool_calls(message):
                    if tool_id := _tool_call_id(tool_call):
                        known_tool_call_ids.add(tool_id)
            elif role == "tool" and has_assistant and known_tool_call_ids:
                tool_call_id = message.get("tool_call_id")
                if tool_call_id and tool_call_id not in known_tool_call_ids:
                    self._record_violation("orphaned_tool_result", index)
                    raise MessageValidationError(
                        f"Message {index} has tool_call_id={tool_call_id!r} with no matching "
                        f"preceding assistant tool_call.",
                        violation="orphaned_tool_result",
                        message_index=index,
                    )

        call_to_assistant: dict[str, int] = {}
        for index, message in enumerate(messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                for tool_call in _get_tool_calls(message):
                    if tool_id := _tool_call_id(tool_call):
                        call_to_assistant[tool_id] = index

        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue

            tool_call_id = message.get("tool_call_id")
            if not tool_call_id or tool_call_id not in call_to_assistant:
                continue

            caller_index = call_to_assistant[tool_call_id]
            for between_index in range(caller_index + 1, index):
                other = messages[between_index]
                if isinstance(other, dict) and other.get("role") == "assistant":
                    self._record_violation("misplaced_tool_result", index)
                    raise MessageValidationError(
                        f"Message {index} tool result for {tool_call_id!r} appears after "
                        f"assistant message at index {between_index}.",
                        violation="misplaced_tool_result",
                        message_index=index,
                    )

        self._audit.append({"event": "validation_ok", "message_count": len(messages), "ts": now})
        return messages

    def repair(self, messages: list[Any]) -> list[Any]:
        """Auto-repair repairable issues. Returns a new list; input is not mutated."""
        now = time.monotonic()
        result = deepcopy(messages)
        repairs: list[dict[str, Any]] = []

        for index, message in enumerate(result):
            if not isinstance(message, dict):
                continue

            role = message.get("role", "")

            if self._strict_roles and role not in _VALID_ROLES:
                self._record_violation("invalid_role", index)
                raise MessageValidationError(
                    f"Message {index} has invalid role {role!r}: cannot infer correct role.",
                    violation="invalid_role",
                    message_index=index,
                )

            tool_calls = _get_tool_calls(message)

            if tool_calls:
                has_fc = any(_is_fc_partial(tool_call) for tool_call in tool_calls)
                has_call = any(_is_call_final(tool_call) for tool_call in tool_calls)
                if has_fc and has_call:
                    cleaned = [
                        tool_call for tool_call in tool_calls if not _is_fc_partial(tool_call)
                    ]
                    message["tool_calls"] = cleaned
                    repairs.append(
                        {
                            "repair": "duplicate_tool_call_blocks",
                            "message_index": index,
                            "dropped": len(tool_calls) - len(cleaned),
                        }
                    )
                    tool_calls = cleaned

                ids = [
                    tool_id
                    for tool_call in tool_calls
                    if (tool_id := _tool_call_id(tool_call))
                ]
                if len(ids) != len(set(ids)):
                    seen: dict[str, Any] = {}
                    for tool_call in tool_calls:
                        if tool_id := _tool_call_id(tool_call):
                            seen[tool_id] = tool_call
                    message["tool_calls"] = list(seen.values())
                    repairs.append({"repair": "duplicate_tool_call_ids", "message_index": index})
                    tool_calls = message["tool_calls"]

                if self._check_indices:
                    indices = [
                        tool_index
                        for tool_call in tool_calls
                        if (tool_index := _tool_call_index(tool_call)) is not None
                    ]
                    if indices and indices[0] != 0:
                        offset = indices[0]
                        for tool_call in tool_calls:
                            if isinstance(tool_call, dict) and tool_call.get("index") is not None:
                                tool_call["index"] = tool_call["index"] - offset
                        repairs.append(
                            {
                                "repair": "nonzero_tool_call_index",
                                "message_index": index,
                                "offset_removed": offset,
                            }
                        )

            if role == "tool" and not message.get("tool_call_id"):
                self._record_violation("missing_tool_call_id", index)
                raise MessageValidationError(
                    f"Message {index} has role='tool' but no tool_call_id.",
                    violation="missing_tool_call_id",
                    message_index=index,
                )

            if self._check_parsed and "parsed" in message:
                for key in ("parsed", "refusal"):
                    message.pop(key, None)
                repairs.append({"repair": "parsed_artifact", "message_index": index})

        for repair in repairs:
            self._audit.append({"event": "repaired", "ts": now, **repair})

        event = "repair_ok" if repairs else "validation_ok"
        self._audit.append(
            {
                "event": event,
                "repairs": len(repairs),
                "message_count": len(result),
                "ts": now,
            }
        )
        return result

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)

    def _record_violation(self, violation: str, index: int) -> None:
        self._audit.append(
            {
                "event": "validation_error",
                "violation": violation,
                "message_index": index,
                "ts": time.monotonic(),
            }
        )
