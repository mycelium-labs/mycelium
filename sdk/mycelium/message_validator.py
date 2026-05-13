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

    # Raises on the first structural error found:
    messages = validator.validate(messages)

    # Or auto-repair what can be fixed, raise only for what cannot:
    messages = validator.repair(messages)
"""

import time
from copy import deepcopy
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

# Violations that repair() can fix automatically.
_REPAIRABLE = {
    "duplicate_tool_call_blocks",
    "duplicate_tool_call_ids",
    "nonzero_tool_call_index",
    "parsed_artifact",
}


def _get_tool_calls(message: dict) -> list:
    return message.get("tool_calls") or []


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
    Validates and optionally repairs message list structure before passing to the LLM.

    validate(messages) — raises MessageValidationError on the first violation found.
    repair(messages)   — auto-fixes what it can; raises only for unrecoverable violations.

    Repairable violations:
      - duplicate_tool_call_blocks  → drop fc_* partials, keep call_* finals
      - duplicate_tool_call_ids     → deduplicate by id, keep last occurrence
      - nonzero_tool_call_index     → re-number indices starting from 0
      - parsed_artifact             → strip `parsed` and `refusal` keys

    Unrecoverable violations (always raise):
      - missing_tool_call_id        → cannot reconstruct the correct id
      - invalid_role                → cannot infer the correct role

    Args:
        strict_roles:   Raise on unknown roles (default True).
        check_indices:  Check/fix non-zero-based tool-call index sequences (default True).
        check_parsed:   Check/fix `parsed` structured-output artifacts (default True).
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
        Validate message list structure. Returns messages unchanged.

        Raises:
            MessageValidationError: on the first structural violation found.
        """
        now = time.monotonic()
        self._audit.append(
            {"event": "validation_started", "message_count": len(messages), "ts": now}
        )

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

            if tool_calls:
                has_fc = any(_is_fc_partial(tc) for tc in tool_calls)
                has_call = any(_is_call_final(tc) for tc in tool_calls)
                if has_fc and has_call:
                    self._record_violation("duplicate_tool_call_blocks", i)
                    raise MessageValidationError(
                        f"Message {i} (role={role!r}) contains both partial fc_* and final call_* "
                        f"tool-call blocks. LangChain streaming produced duplicate entries — "
                        f"call repair() to drop the fc_* partials automatically.",
                        violation="duplicate_tool_call_blocks",
                        message_index=i,
                    )

                ids = [_tool_call_id(tc) for tc in tool_calls if _tool_call_id(tc)]
                if len(ids) != len(set(ids)):
                    self._record_violation("duplicate_tool_call_ids", i)
                    raise MessageValidationError(
                        f"Message {i} (role={role!r}) has duplicate tool_call ids: {ids}.",
                        violation="duplicate_tool_call_ids",
                        message_index=i,
                    )

                if self._check_indices:
                    indices = [
                        _tool_call_index(tc)
                        for tc in tool_calls
                        if _tool_call_index(tc) is not None
                    ]
                    if indices and indices[0] != 0:
                        self._record_violation("nonzero_tool_call_index", i)
                        raise MessageValidationError(
                            f"Message {i} tool_calls start at index {indices[0]}, expected 0. "
                            f"Non-zero indices cause tool-result mismatches.",
                            violation="nonzero_tool_call_index",
                            message_index=i,
                        )

            if role == "tool" and not msg.get("tool_call_id"):
                self._record_violation("missing_tool_call_id", i)
                raise MessageValidationError(
                    f"Message {i} has role='tool' but no tool_call_id. "
                    f"The LLM cannot match this response to the originating call.",
                    violation="missing_tool_call_id",
                    message_index=i,
                )

            if self._check_parsed and "parsed" in msg:
                self._record_violation("parsed_artifact", i)
                raise MessageValidationError(
                    f"Message {i} contains a 'parsed' field — structured output artifact "
                    f"left in message history. Call repair() to strip it automatically.",
                    violation="parsed_artifact",
                    message_index=i,
                )

        # Orphaned tool-result detection: every role="tool" must match a tool_call.id
        # from a *preceding* role="assistant" message.
        known_tool_call_ids: set[str] = set()
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            if role == "assistant":
                for tc in _get_tool_calls(msg):
                    tid = _tool_call_id(tc)
                    if tid:
                        known_tool_call_ids.add(tid)
            elif role == "tool":
                tcid = msg.get("tool_call_id")
                if tcid and tcid not in known_tool_call_ids:
                    self._record_violation("orphaned_tool_result", i)
                    raise MessageValidationError(
                        f"Message {i} has role='tool' with tool_call_id={tcid!r}, "
                        f"but no preceding assistant message contains a matching tool_call. "
                        f"The assistant message may have been dropped or the id corrupted.",
                        violation="orphaned_tool_result",
                        message_index=i,
                    )

        # Misplaced tool-result detection: every role="tool" must appear before
        # any subsequent role="assistant" that doesn't reference its call_id.
        call_to_assistant: dict[str, int] = {}
        for i, msg in enumerate(messages):
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                for tc in _get_tool_calls(msg):
                    tid = _tool_call_id(tc)
                    if tid:
                        call_to_assistant[tid] = i

        for i, msg in enumerate(messages):
            if not isinstance(msg, dict) or msg.get("role") != "tool":
                continue
            tcid = msg.get("tool_call_id")
            if not tcid or tcid not in call_to_assistant:
                continue
            caller_idx = call_to_assistant[tcid]
            for k in range(caller_idx + 1, i):
                other = messages[k]
                if isinstance(other, dict) and other.get("role") == "assistant":
                    self._record_violation("misplaced_tool_result", i)
                    raise MessageValidationError(
                        f"Message {i} has role='tool' with tool_call_id={tcid!r}, "
                        f"but an assistant message at index {k} appears between it "
                        f"and the call at index {caller_idx}. The tool result was "
                        f"placed after a subsequent assistant response.",
                        violation="misplaced_tool_result",
                        message_index=i,
                    )

        self._audit.append({"event": "validation_ok", "message_count": len(messages), "ts": now})
        return messages

    def repair(self, messages: list) -> list:
        """
        Auto-repair message list structure where possible.

        Fixes repairable violations in-place on a deep copy and returns the
        cleaned list. Raises MessageValidationError for unrecoverable violations
        (missing_tool_call_id, invalid_role) where the correct value cannot be
        inferred.

        Repairs applied:
          duplicate_tool_call_blocks — drop fc_* partials, keep call_* finals
          duplicate_tool_call_ids    — deduplicate by id, keep last occurrence
          nonzero_tool_call_index    — re-number indices starting from 0
          parsed_artifact            — strip `parsed` and `refusal` keys

        Returns:
            Repaired copy of messages (input is never mutated).

        Raises:
            MessageValidationError: for unrecoverable violations.
        """
        now = time.monotonic()
        result = deepcopy(messages)
        repairs: list[dict[str, Any]] = []

        for i, msg in enumerate(result):
            if not isinstance(msg, dict):
                continue

            role = msg.get("role", "")

            # Unrecoverable: invalid role
            if self._strict_roles and role not in _VALID_ROLES:
                self._record_violation("invalid_role", i)
                raise MessageValidationError(
                    f"Message {i} has invalid role {role!r} — cannot infer correct role.",
                    violation="invalid_role",
                    message_index=i,
                )

            tool_calls = _get_tool_calls(msg)

            if tool_calls:
                # Repair: drop fc_* partials when call_* finals are present
                has_fc = any(_is_fc_partial(tc) for tc in tool_calls)
                has_call = any(_is_call_final(tc) for tc in tool_calls)
                if has_fc and has_call:
                    cleaned = [tc for tc in tool_calls if not _is_fc_partial(tc)]
                    msg["tool_calls"] = cleaned
                    repairs.append(
                        {
                            "repair": "duplicate_tool_call_blocks",
                            "message_index": i,
                            "dropped": len(tool_calls) - len(cleaned),
                        }
                    )
                    tool_calls = cleaned

                # Repair: deduplicate by id (keep last occurrence)
                ids = [_tool_call_id(tc) for tc in tool_calls if _tool_call_id(tc)]
                if len(ids) != len(set(ids)):
                    seen: dict[str, Any] = {}
                    for tc in tool_calls:
                        tid = _tool_call_id(tc)
                        if tid:
                            seen[tid] = tc
                    msg["tool_calls"] = list(seen.values())
                    repairs.append({"repair": "duplicate_tool_call_ids", "message_index": i})
                    tool_calls = msg["tool_calls"]

                # Repair: re-number indices from 0
                if self._check_indices:
                    indices = [
                        _tool_call_index(tc)
                        for tc in tool_calls
                        if _tool_call_index(tc) is not None
                    ]
                    if indices and indices[0] != 0:
                        offset = indices[0]
                        for tc in tool_calls:
                            if isinstance(tc, dict) and tc.get("index") is not None:
                                tc["index"] = tc["index"] - offset
                        repairs.append(
                            {
                                "repair": "nonzero_tool_call_index",
                                "message_index": i,
                                "offset_removed": offset,
                            }
                        )

            # Unrecoverable: missing tool_call_id
            if role == "tool" and not msg.get("tool_call_id"):
                self._record_violation("missing_tool_call_id", i)
                raise MessageValidationError(
                    f"Message {i} has role='tool' but no tool_call_id — cannot reconstruct "
                    f"which tool call this response belongs to.",
                    violation="missing_tool_call_id",
                    message_index=i,
                )

            # Repair: strip parsed/refusal structured-output artifacts
            if self._check_parsed and "parsed" in msg:
                for key in ("parsed", "refusal"):
                    msg.pop(key, None)
                repairs.append({"repair": "parsed_artifact", "message_index": i})

        for r in repairs:
            self._audit.append({"event": "repaired", "ts": now, **r})

        if repairs:
            self._audit.append(
                {
                    "event": "repair_ok",
                    "repairs": len(repairs),
                    "message_count": len(result),
                    "ts": now,
                }
            )
        else:
            self._audit.append({"event": "validation_ok", "message_count": len(result), "ts": now})

        return result

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _record_violation(self, violation: str, index: int) -> None:
        self._audit.append(
            {
                "event": "validation_error",
                "violation": violation,
                "message_index": index,
                "ts": time.monotonic(),
            }
        )
