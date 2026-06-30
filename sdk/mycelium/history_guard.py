"""
HistoryGuard: validates message history before LLM calls.

Covers token overflow, silent message drops, and duplicate turns.
"""

from __future__ import annotations

import hashlib
import time
from typing import Any


class HistoryTruncatedError(Exception):
    """Raised when message history exceeds limits or messages were dropped."""

    def __init__(self, reason: str, message_count: int, estimated_tokens: int) -> None:
        super().__init__(reason)
        self.message_count = message_count
        self.estimated_tokens = estimated_tokens


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
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
    content = getattr(message, "content", None)
    if content is not None:
        return _extract_text(content)
    return str(message)


def _message_role(message: Any) -> str:
    if isinstance(message, dict):
        return message.get("role", "unknown")
    return getattr(message, "type", getattr(message, "role", "unknown"))


def estimate_tokens(messages: list[Any]) -> int:
    """Rough token estimate without external tokenizers (~4 chars per token)."""
    total = 3
    for message in messages:
        total += 4
        total += max(1, len(_message_text(message)) // 4)
    return total


def _fingerprint(message: Any) -> str:
    role = _message_role(message)
    text = _message_text(message)
    raw = f"{role}:{text[:200]}"
    return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()


class HistoryGuard:
    """
    Validates message history before each LLM call.

    Args:
        max_tokens: Raise if estimated token count exceeds this value.
        max_messages: Raise if message count exceeds this value.
        warn_at: Fraction of max_tokens for near-limit audit events (default 0.9).
        detect_duplicates: Raise when duplicate message fingerprints appear.
    """

    def __init__(
        self,
        max_tokens: int | None = None,
        max_messages: int | None = None,
        *,
        warn_at: float = 0.9,
        detect_duplicates: bool = True,
    ) -> None:
        self._max_tokens = max_tokens
        self._max_messages = max_messages
        self._warn_at = warn_at
        self._detect_duplicates = detect_duplicates
        self._last_fingerprints: list[str] = []
        self._audit: list[dict[str, Any]] = []

    def validate(self, messages: list[Any]) -> list[Any]:
        """
        Validate messages before sending to the LLM.

        Records fingerprints so check_for_drops() can detect silent drops later.
        Returns messages unchanged.
        """
        now = time.monotonic()
        count = len(messages)
        tokens = estimate_tokens(messages)

        self._audit.append(
            {
                "event": "history_checked",
                "message_count": count,
                "estimated_tokens": tokens,
                "ts": now,
            }
        )

        if self._max_tokens is not None and tokens > self._max_tokens:
            self._audit.append(
                {
                    "event": "history_truncated",
                    "reason": "token_limit",
                    "estimated_tokens": tokens,
                    "max_tokens": self._max_tokens,
                    "ts": now,
                }
            )
            raise HistoryTruncatedError(
                f"Message history would exceed token limit: "
                f"~{tokens} estimated tokens > {self._max_tokens} max.",
                message_count=count,
                estimated_tokens=tokens,
            )

        if self._max_tokens is not None and tokens >= self._max_tokens * self._warn_at:
            self._audit.append(
                {
                    "event": "history_near_limit",
                    "estimated_tokens": tokens,
                    "max_tokens": self._max_tokens,
                    "pct": round(tokens / self._max_tokens, 2),
                    "ts": now,
                }
            )

        if self._max_messages is not None and count > self._max_messages:
            self._audit.append(
                {
                    "event": "history_truncated",
                    "reason": "message_count",
                    "message_count": count,
                    "max_messages": self._max_messages,
                    "ts": now,
                }
            )
            raise HistoryTruncatedError(
                f"Message history exceeds max_messages: {count} > {self._max_messages}.",
                message_count=count,
                estimated_tokens=tokens,
            )

        if self._detect_duplicates:
            seen: set[str] = set()
            duplicate_indices: list[int] = []
            for index, fingerprint in enumerate(_fingerprint(message) for message in messages):
                if fingerprint in seen:
                    duplicate_indices.append(index)
                seen.add(fingerprint)
            if duplicate_indices:
                self._audit.append(
                    {
                        "event": "history_duplicate_turns",
                        "duplicate_indices": duplicate_indices,
                        "duplicate_count": len(duplicate_indices),
                        "ts": now,
                    }
                )
                raise HistoryTruncatedError(
                    f"Duplicate turns detected at index(s) {duplicate_indices}.",
                    message_count=count,
                    estimated_tokens=tokens,
                )

        self._last_fingerprints = [_fingerprint(message) for message in messages]
        self._audit.append({"event": "history_ok", "message_count": count, "ts": now})
        return messages

    def check_for_drops(self, messages: list[Any]) -> None:
        """
        Detect messages silently dropped since the last validate() call.

        Raises RuntimeError if validate() has not been called yet.
        """
        if not self._last_fingerprints:
            raise RuntimeError(
                "check_for_drops() called before validate(). Call validate(messages) first."
            )

        now = time.monotonic()
        current_fingerprints = {_fingerprint(message) for message in messages}
        dropped = [
            fingerprint
            for fingerprint in self._last_fingerprints
            if fingerprint not in current_fingerprints
        ]

        if dropped:
            self._audit.append(
                {
                    "event": "history_drop_detected",
                    "dropped_count": len(dropped),
                    "remaining_count": len(messages),
                    "ts": now,
                }
            )
            raise HistoryTruncatedError(
                f"{len(dropped)} message(s) were silently dropped from the history. "
                f"Had {len(self._last_fingerprints)} messages, now have {len(messages)}.",
                message_count=len(messages),
                estimated_tokens=estimate_tokens(messages),
            )

        self._audit.append(
            {
                "event": "history_drop_check_ok",
                "message_count": len(messages),
                "ts": now,
            }
        )

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)
