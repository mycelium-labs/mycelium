"""
HistoryGuard — protection against context truncation (AF-006 message history layer).

Covers the largest unaddressed AF-006 sub-type: agents that silently lose earlier
messages when the conversation history grows past the model's token limit. The agent
continues reasoning as if it has full context — it doesn't.

Two failure modes addressed:
  1. Token overflow  — history is too large to send; framework silently drops messages.
  2. Silent drop     — message count decreases between turns without the agent noticing.

Usage:
    from mycelium import HistoryGuard, HistoryTruncatedError

    guard = HistoryGuard(max_tokens=8192)

    # Before each LLM call — raises HistoryTruncatedError if over limit:
    messages = guard.validate(messages)

    # After framework processes messages but before sending — detects silent drops:
    guard.check_for_drops(processed_messages)
"""

import hashlib
import time
from typing import Any

from mycelium.protect import _session_var


class HistoryTruncatedError(Exception):
    """
    Raised when the message history has been truncated or would exceed the token limit.
    Carries .message_count and .estimated_tokens so the caller can decide how to recover.
    """

    def __init__(self, reason: str, message_count: int, estimated_tokens: int) -> None:
        super().__init__(reason)
        self.message_count = message_count
        self.estimated_tokens = estimated_tokens


# ---------------------------------------------------------------------------
# Token estimation (no external dependency)
# ---------------------------------------------------------------------------

def _extract_text(content: Any) -> str:
    """Pull plain text out of a message content field regardless of format."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Anthropic / OpenAI structured content blocks
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text") or block.get("content") or "")
            else:
                parts.append(str(block))
        return " ".join(parts)
    return str(content)


def _message_text(message: Any) -> str:
    if isinstance(message, dict):
        return _extract_text(message.get("content", ""))
    # LangChain / other message objects
    content = getattr(message, "content", None)
    if content is not None:
        return _extract_text(content)
    return str(message)


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return message.get("role", "unknown")
    return getattr(message, "type", getattr(message, "role", "unknown"))


def estimate_tokens(messages: list) -> int:
    """
    Rough token estimate without tiktoken.

    Approximates GPT-4 tokenisation: ~4 chars per token + 4 overhead per message.
    Accurate to ±15% for typical English conversation histories.
    """
    total = 3  # priming tokens
    for m in messages:
        total += 4  # per-message overhead (role + delimiters)
        total += max(1, len(_message_text(m)) // 4)
    return total


# ---------------------------------------------------------------------------
# Message fingerprinting for drop detection
# ---------------------------------------------------------------------------

def _fingerprint(message: Any) -> str:
    role = _message_role(message)
    text = _message_text(message)
    raw = f"{role}:{text[:200]}"  # first 200 chars is enough to identify a message
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


# ---------------------------------------------------------------------------
# HistoryGuard
# ---------------------------------------------------------------------------

def _try_active_session() -> Any:
    try:
        return _session_var.get()
    except LookupError:
        return None


class HistoryGuard:
    """
    Validates message history before each LLM call.

    Args:
        max_tokens:    Raise HistoryTruncatedError if estimated token count exceeds
                       this value. None = no token-limit check.
        max_messages:  Raise HistoryTruncatedError if message count exceeds this value.
                       None = no count limit.
        warn_at:       Fraction of max_tokens at which to emit a `history_near_limit`
                       audit event (default 0.9 = 90%).
    """

    def __init__(
        self,
        max_tokens: int | None = None,
        max_messages: int | None = None,
        warn_at: float = 0.9,
    ) -> None:
        self._max_tokens = max_tokens
        self._max_messages = max_messages
        self._warn_at = warn_at
        self._last_fingerprints: list[str] = []
        self._audit: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def validate(self, messages: list) -> list:
        """
        Validate messages before sending to the LLM.

        - Estimates token count and raises if over max_tokens.
        - Raises if message count exceeds max_messages.
        - Records message fingerprints so check_for_drops() can detect silent drops.
        - Returns messages unchanged (pure validation, no mutation).

        Raises:
            HistoryTruncatedError: history exceeds configured limits.
        """
        now = time.monotonic()
        count = len(messages)
        tokens = estimate_tokens(messages)

        self._audit.append({
            "event": "history_checked",
            "message_count": count,
            "estimated_tokens": tokens,
            "ts": now,
        })
        self._log_to_session({"event": "history_checked", "message_count": count,
                               "estimated_tokens": tokens, "ts": now})

        # Token limit check
        if self._max_tokens is not None:
            if tokens > self._max_tokens:
                self._audit.append({
                    "event": "history_truncated",
                    "reason": "token_limit",
                    "estimated_tokens": tokens,
                    "max_tokens": self._max_tokens,
                    "ts": now,
                })
                self._log_to_session({"event": "history_truncated", "reason": "token_limit",
                                       "estimated_tokens": tokens, "ts": now})
                raise HistoryTruncatedError(
                    f"Message history would exceed token limit: "
                    f"~{tokens} estimated tokens > {self._max_tokens} max. "
                    f"Summarise or compress earlier messages before sending.",
                    message_count=count,
                    estimated_tokens=tokens,
                )
            if tokens >= self._max_tokens * self._warn_at:
                self._audit.append({
                    "event": "history_near_limit",
                    "estimated_tokens": tokens,
                    "max_tokens": self._max_tokens,
                    "pct": round(tokens / self._max_tokens, 2),
                    "ts": now,
                })
                self._log_to_session({"event": "history_near_limit", "estimated_tokens": tokens,
                                       "pct": round(tokens / self._max_tokens, 2), "ts": now})

        # Message count limit check
        if self._max_messages is not None and count > self._max_messages:
            self._audit.append({
                "event": "history_truncated",
                "reason": "message_count",
                "message_count": count,
                "max_messages": self._max_messages,
                "ts": now,
            })
            self._log_to_session({"event": "history_truncated", "reason": "message_count",
                                   "message_count": count, "ts": now})
            raise HistoryTruncatedError(
                f"Message history exceeds max_messages: "
                f"{count} messages > {self._max_messages} max.",
                message_count=count,
                estimated_tokens=tokens,
            )

        # Record fingerprints for drop detection
        self._last_fingerprints = [_fingerprint(m) for m in messages]

        self._audit.append({"event": "history_ok", "message_count": count, "ts": now})
        self._log_to_session({"event": "history_ok", "message_count": count, "ts": now})
        return messages

    def check_for_drops(self, messages: list) -> None:
        """
        Check whether messages were silently dropped since the last validate() call.

        Call this after a framework has processed the history but before the LLM
        response arrives. If any previously-seen messages are missing, raises
        HistoryTruncatedError.

        Raises:
            HistoryTruncatedError: one or more messages were dropped.
            RuntimeError: called before validate() was ever called.
        """
        if not self._last_fingerprints:
            raise RuntimeError(
                "check_for_drops() called before validate(). "
                "Call validate(messages) first."
            )

        now = time.monotonic()
        current_fingerprints = {_fingerprint(m) for m in messages}
        dropped = [fp for fp in self._last_fingerprints if fp not in current_fingerprints]

        if dropped:
            self._audit.append({
                "event": "history_drop_detected",
                "dropped_count": len(dropped),
                "remaining_count": len(messages),
                "ts": now,
            })
            self._log_to_session({
                "event": "history_drop_detected",
                "dropped_count": len(dropped),
                "ts": now,
            })
            raise HistoryTruncatedError(
                f"{len(dropped)} message(s) were silently dropped from the history. "
                f"Had {len(self._last_fingerprints)} messages, now have {len(messages)}.",
                message_count=len(messages),
                estimated_tokens=estimate_tokens(messages),
            )

        self._audit.append({
            "event": "history_drop_check_ok",
            "message_count": len(messages),
            "ts": now,
        })

    def estimate_tokens(self, messages: list) -> int:
        """Estimate token count for the given message list."""
        return estimate_tokens(messages)

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _log_to_session(self, entry: dict[str, Any]) -> None:
        session = _try_active_session()
        if session is not None:
            session._audit.append(entry)
