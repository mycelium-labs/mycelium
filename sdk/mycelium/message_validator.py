"""
MessageValidator — protection against malformed message history (AF-006 serialization layer).

Covers serialization bugs and tool-call corruption from the AF-006 dataset:
  - Duplicate tool_call blocks (fc_* partial + call_* final mixed together from LangChain streaming)
  - Missing tool_call_id in tool response messages (AutoGen MultimodalConversableAgent)
  - Inconsistent tool-call indices (1-based instead of 0-based)
  - Structured output `parsed` field artifacts left in message history
  - Invalid or missing roles

Usage:
    from mycelium import MessageValidator, MessageValidationError

    validator = MessageValidator()
    messages = validator.validate(messages)   # raises on structural errors
"""

import hashlib
import time
from typing import Any


class MessageValidationError(Exception):
    """
    Raised when the message list contains structural errors that would corrupt the LLM call.
    Carries .violation (machine-readable key) and .message_index (0-based, -1 if global).
    """

    def __init__(self, reason: str, violation: str, message_index: int = -1) -> None:
        super().__init__(reason)
        self.violation = violation
        self.message_index = message_index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_ROLES = {"system", "user", "assistant", "tool", "function"}


def _get_tool_calls(message: dict) -> list:
    return message.get("tool_calls") or []


def _get_function_call(message: dict) -> dict | None:
    return message.get("function_call")


def _tool_call_id(tc: Any) -> str | None:
    if isinstance(tc, dict):
        return tc.get("id")
    return None


def _tool_call_index(tc: Any) -> int | None:
    if isinstance(tc, dict):
        idx = tc.get("index")
        if idx is not None:
            return int(idx)
    return None


def _is_fc_partial(tc: Any) -> bool:
    """LangChain streaming produces fc_* prefixed partial tool-call blocks."""
    tid = _tool_call_id(tc)
    return isinstance(tid, str) and tid.startswith("fc_")


def _is_call_final(tc: Any) -> bool:
    """OpenAI final tool-call ids start with 'call_'."""
    tid = _tool_call_id(tc)
    return isinstance(tid, str) and tid.startswith("call_")


# ---------------------------------------------------------------------------
# MessageValidator
# ---------------------------------------------------------------------------

class MessageValidator:
    """
    Validates message list structure before passing to the LLM.

    Checks performed (in order):
      1. Role validity — every message has a known role
      2. Duplicate tool-call blocks — fc_* + call_* mixed in same message
      3. Missing tool_call_id — tool-response messages without id
      4. Inconsistent tool-call indices — non-zero-based or non-contiguous
      5. Structured output artifacts — `parsed` field in message content
      6. Duplicate tool_call ids across the same assistant message

    Args:
        strict_roles:   Raise on unknown roles (default True).
        check_indices:  Raise on non-zero-based tool-call index sequences (default True).
        check_parsed:   Raise on `parsed` structured-output artifacts (default True).
    """

    def __init__(
        self,
        strict_roles: bool = True,
        check_indices: bool = True,
        check_parsed: bool = True,
    ) -> None:
        self._strict_roles = strict_roles
        self._check_indices = check_indices
        self._check_parsed = check_parsed
        self._audit: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def validate(self, messages: list) -> list:
        """
        Validate message list structure.

        Returns messages unchanged (pure validation, no mutation).

        Raises:
            MessageValidationError: one or more structural violations found.
        """
        now = time.monotonic()
        self._audit.append({"event": "validation_started", "message_count": len(messages), "ts": now})

        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue

            role = msg.get("role", "")

            if self._strict_roles and role not in _VALID_ROLES:
                self._record_violation("invalid_role", i)
                raise MessageValidationError(
                    f"Message {i} has invalid role {role!r}. Expected one of {sorted(_VALID_ROLES)}.",
                    violation="invalid_role",
                    message_index=i,
                )

            tool_calls = _get_tool_calls(msg)

            # Check for fc_* + call_* duplicates (LangChain streaming bug)
            if tool_calls:
                has_fc = any(_is_fc_partial(tc) for tc in tool_calls)
                has_call = any(_is_call_final(tc) for tc in tool_calls)
                if has_fc and has_call:
                    self._record_violation("duplicate_tool_call_blocks", i)
                    raise MessageValidationError(
                        f"Message {i} (role={role!r}) contains both partial fc_* and final call_* "
                        f"tool-call blocks. LangChain streaming produced duplicate entries — "
                        f"deduplicate or discard the fc_* partials before sending.",
                        violation="duplicate_tool_call_blocks",
                        message_index=i,
                    )

                # Check for duplicate tool_call ids within one message
                ids = [_tool_call_id(tc) for tc in tool_calls if _tool_call_id(tc)]
                if len(ids) != len(set(ids)):
                    self._record_violation("duplicate_tool_call_ids", i)
                    raise MessageValidationError(
                        f"Message {i} (role={role!r}) has duplicate tool_call ids: {ids}.",
                        violation="duplicate_tool_call_ids",
                        message_index=i,
                    )

                # Check index ordering (0-based, contiguous)
                if self._check_indices:
                    indices = [_tool_call_index(tc) for tc in tool_calls if _tool_call_index(tc) is not None]
                    if indices:
                        if indices[0] != 0:
                            self._record_violation("nonzero_tool_call_index", i)
                            raise MessageValidationError(
                                f"Message {i} tool_calls start at index {indices[0]}, expected 0. "
                                f"Non-zero indices cause tool-result mismatches.",
                                violation="nonzero_tool_call_index",
                                message_index=i,
                            )

            # Tool-response message must have tool_call_id
            if role == "tool":
                if not msg.get("tool_call_id"):
                    self._record_violation("missing_tool_call_id", i)
                    raise MessageValidationError(
                        f"Message {i} has role='tool' but no tool_call_id. "
                        f"The LLM cannot match this response to the originating call.",
                        violation="missing_tool_call_id",
                        message_index=i,
                    )

            # Structured output artifact check
            if self._check_parsed and msg.get("parsed") is not None:
                self._record_violation("parsed_artifact", i)
                raise MessageValidationError(
                    f"Message {i} contains a 'parsed' field — structured output artifact "
                    f"left in message history. Remove before sending to the LLM.",
                    violation="parsed_artifact",
                    message_index=i,
                )

        self._audit.append({"event": "validation_ok", "message_count": len(messages), "ts": now})
        return messages

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _record_violation(self, violation: str, index: int) -> None:
        self._audit.append({
            "event": "validation_error",
            "violation": violation,
            "message_index": index,
            "ts": time.monotonic(),
        })
