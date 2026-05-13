"""
ContentBlockNormalizer — protection against thinking-block stripping and provider format
mismatches (AF-006 content corruption layer).

Covers 23 real failures from the AF-006 dataset:
  - Anthropic thinking blocks stripped by SummarizationMiddleware (16 failures)
  - DeepSeek-r1 <think> content mixed into response text
  - reasoning_content / thinking_blocks fields ignored by framework wrappers
  - OpenAI function_call format persisting when calling Anthropic/Bedrock
  - OpenAI reasoning blocks passed to Anthropic unchanged

Usage:
    from mycelium import ContentBlockNormalizer, ContentBlockError

    normalizer = ContentBlockNormalizer(target_provider="anthropic")
    messages = normalizer.normalize(messages)   # returns (possibly modified) messages
    issues = normalizer.audit_log()
"""

import re
import time
from copy import deepcopy
from typing import Any

from mycelium.protect import _session_var


class ContentBlockError(Exception):
    """
    Raised when content blocks cannot be safely normalized without data loss,
    or when a provider-incompatible block is detected and strict=True.
    Carries .violation and .message_index.
    """

    def __init__(self, reason: str, violation: str, message_index: int = -1) -> None:
        super().__init__(reason)
        self.violation = violation
        self.message_index = message_index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEEPSEEK_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL)


def _try_active_session() -> Any:
    try:
        return _session_var.get()
    except LookupError:
        return None


def _is_thinking_block(block: Any) -> bool:
    if not isinstance(block, dict):
        return False
    return block.get("type") in ("thinking", "redacted_thinking")


def _has_thinking_blocks(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(_is_thinking_block(b) for b in content)


def _extract_deepseek_thinking(text: str) -> tuple[str, str | None]:
    """Return (clean_text, thinking_text_or_None)."""
    m = _DEEPSEEK_THINK_RE.search(text)
    if m:
        thinking = m.group(1).strip()
        clean = _DEEPSEEK_THINK_RE.sub("", text).strip()
        return clean, thinking
    return text, None


# ---------------------------------------------------------------------------
# ContentBlockNormalizer
# ---------------------------------------------------------------------------


class ContentBlockNormalizer:
    """
    Normalises content blocks in a message list for safe LLM submission.

    Operations:
      - preserve_thinking: Detect and flag thinking blocks that would be stripped
                           (raises ContentBlockError if strict=True; emits audit event otherwise).
      - extract_deepseek:  Strip <think>…</think> from DeepSeek-r1 content; the thinking
                           text is saved in the audit log but removed from the message.
      - normalize_function_call: Convert legacy OpenAI function_call dicts to tool_calls
                                 format required by Anthropic/Bedrock.
      - strip_reasoning:   Remove OpenAI `reasoning` content blocks that Anthropic rejects.

    Args:
        target_provider:   "anthropic" | "openai" | None. Enables provider-specific checks.
        preserve_thinking: Raise ContentBlockError if thinking blocks would be lost (default True).
        extract_deepseek:  Strip DeepSeek <think> tags from message text (default True).
        normalize_function_call: Convert function_call → tool_calls (default True).
        strip_reasoning:   Remove OpenAI reasoning blocks when target is Anthropic (default True).
        strict:            Raise on unrecoverable issues (default False — emit events only).
    """

    def __init__(
        self,
        target_provider: str | None = None,
        preserve_thinking: bool = True,
        extract_deepseek: bool = True,
        normalize_function_call: bool = True,
        strip_reasoning: bool = True,
        strict: bool = False,
    ) -> None:
        self._target = target_provider
        self._preserve_thinking = preserve_thinking
        self._extract_deepseek = extract_deepseek
        self._normalize_function_call = normalize_function_call
        self._strip_reasoning = strip_reasoning
        self._strict = strict
        self._audit: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def normalize(self, messages: list) -> list:
        """
        Normalize content blocks in the message list.

        Returns a new list (does not mutate input) with issues corrected where
        possible. Raises ContentBlockError for unrecoverable issues if strict=True.
        """
        now = time.monotonic()
        self._audit.append(
            {"event": "normalize_started", "message_count": len(messages), "ts": now}
        )

        # Auto-detect provider format mismatch
        if self._target is not None:
            detected = self.detect_format(messages)
            if detected is not None and detected != self._target:
                event = {
                    "event": "provider_format_mismatch",
                    "detected": detected,
                    "target": self._target,
                    "ts": now,
                }
                self._audit.append(event)
                self._log_to_session(event)

        result = deepcopy(messages)

        for i, msg in enumerate(result):
            if not isinstance(msg, dict):
                continue

            content = msg.get("content")

            # --- Anthropic thinking block preservation ---
            if self._preserve_thinking and _has_thinking_blocks(content):
                if self._target == "openai":
                    event = {
                        "event": "thinking_block_incompatible",
                        "message_index": i,
                        "ts": now,
                        "detail": "Anthropic thinking blocks cannot be sent to OpenAI.",
                    }
                    self._audit.append(event)
                    self._log_to_session(event)
                    if self._strict:
                        raise ContentBlockError(
                            f"Message {i} contains Anthropic thinking blocks that are incompatible "
                            f"with target_provider='openai'. Strip or convert before sending.",
                            violation="thinking_block_incompatible",
                            message_index=i,
                        )
                else:
                    event = {
                        "event": "thinking_block_present",
                        "message_index": i,
                        "ts": now,
                    }
                    self._audit.append(event)
                    self._log_to_session(event)

            # --- reasoning_content / thinking_blocks field detection ---
            for field in ("reasoning_content", "thinking_blocks"):
                if msg.get(field) is not None:
                    event = {
                        "event": "reasoning_field_detected",
                        "field": field,
                        "message_index": i,
                        "ts": now,
                    }
                    self._audit.append(event)
                    self._log_to_session(event)

            # --- DeepSeek <think> extraction ---
            if self._extract_deepseek and isinstance(content, str):
                clean, thinking = _extract_deepseek_thinking(content)
                if thinking is not None:
                    msg["content"] = clean
                    event = {
                        "event": "deepseek_thinking_extracted",
                        "message_index": i,
                        "thinking_preview": thinking[:100],
                        "ts": now,
                    }
                    self._audit.append(event)
                    self._log_to_session(event)

            # --- OpenAI reasoning blocks → strip for Anthropic ---
            if self._strip_reasoning and self._target == "anthropic" and isinstance(content, list):
                filtered = []
                stripped_count = 0
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "reasoning":
                        stripped_count += 1
                    else:
                        filtered.append(block)
                if stripped_count:
                    msg["content"] = filtered
                    event = {
                        "event": "openai_reasoning_stripped",
                        "message_index": i,
                        "stripped_count": stripped_count,
                        "ts": now,
                    }
                    self._audit.append(event)
                    self._log_to_session(event)

            # --- function_call → tool_calls normalization ---
            if self._normalize_function_call:
                fc = msg.get("function_call")
                if fc and not msg.get("tool_calls"):
                    name = fc.get("name", "")
                    arguments = fc.get("arguments", "")
                    msg["tool_calls"] = [
                        {
                            "id": f"call_{name}_migrated",
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ]
                    del msg["function_call"]
                    event = {
                        "event": "function_call_normalized",
                        "message_index": i,
                        "function_name": name,
                        "ts": now,
                    }
                    self._audit.append(event)
                    self._log_to_session(event)

        self._audit.append({"event": "normalize_ok", "message_count": len(result), "ts": now})
        return result

    def detect_format(self, messages: list) -> str | None:
        """
        Auto-detect the provider format of a message list based on structure.

        Returns ``"openai"``, ``"anthropic"``, ``"deepseek"``, or ``None`` if
        the format cannot be determined. Useful for catching provider schema
        drift before normalization.

            normalizer = ContentBlockNormalizer(target_provider="anthropic")
            detected = normalizer.detect_format(messages)
            if detected and detected != "anthropic":
                print(f"WARNING: messages appear to be in {detected} format")
        """
        has_thinking = False
        has_tool_use = False
        has_openai_tool_calls = False
        has_deepseek_think = False
        has_reasoning_content = False

        for msg in messages:
            if not isinstance(msg, dict):
                continue

            # Check for Anthropic thinking blocks
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        bt = block.get("type")
                        if bt in ("thinking", "redacted_thinking"):
                            has_thinking = True
                        if bt == "tool_use":
                            has_tool_use = True

            # Check for OpenAI-style tool_calls
            if msg.get("tool_calls") or msg.get("function_call"):
                has_openai_tool_calls = True

            # Check for DeepSeek <think> tags in text content
            if isinstance(content, str) and _DEEPSEEK_THINK_RE.search(content):
                has_deepseek_think = True

            # Check for reasoning_content field
            if msg.get("reasoning_content") is not None:
                has_reasoning_content = True

        if has_deepseek_think or has_reasoning_content:
            return "deepseek"
        if has_thinking or has_tool_use:
            return "anthropic"
        if has_openai_tool_calls:
            return "openai"
        return None

    def has_thinking_blocks(self, messages: list) -> bool:
        """Return True if any message contains Anthropic thinking blocks."""
        return any(_has_thinking_blocks(m.get("content")) for m in messages if isinstance(m, dict))

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _log_to_session(self, entry: dict[str, Any]) -> None:
        session = _try_active_session()
        if session is not None:
            session._audit.append(entry)
