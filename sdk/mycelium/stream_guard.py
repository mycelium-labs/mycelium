"""
StreamGuard — protection against streaming corruption.

Handles two AF-006 streaming failure classes:

  1. Cut-off streams   — stream ends without a proper stop signal; the agent
                         treats a partial response as complete.
  2. Duplicate chunks  — the same content chunk is sent twice (exact duplicate
                         or partial replay); the agent sees repeated text.

Usage:
    from mycelium import StreamGuard, StreamCutOffError

    async with StreamGuard(format="openai") as guard:
        async for chunk in llm.astream(prompt):
            chunk = guard.process(chunk)
            if chunk is not None:           # None = duplicate, skip it
                yield chunk
    # raises StreamCutOffError if stream ended without a stop signal

Formats with built-in stop-signal detection:
    "openai"     — detects choices[0].finish_reason is not None
    "anthropic"  — detects message_stop event or message_delta.stop_reason

Custom format:
    async with StreamGuard(stop_validator=lambda c: c.get("done") is True) as guard:
        ...
"""

import hashlib
import time
from collections.abc import Callable
from typing import Any

from mycelium.protect import _session_var


class StreamCutOffError(Exception):
    """Stream ended without a recognised stop signal."""


# ---------------------------------------------------------------------------
# Format adapters
# ---------------------------------------------------------------------------


class _OpenAIAdapter:
    @staticmethod
    def is_stop(chunk: Any) -> bool:
        choices = (
            chunk.get("choices", []) if isinstance(chunk, dict) else getattr(chunk, "choices", [])
        )
        if not choices:
            return False
        choice = choices[0]
        finish_reason = (
            choice.get("finish_reason")
            if isinstance(choice, dict)
            else getattr(choice, "finish_reason", None)
        )
        return finish_reason is not None

    @staticmethod
    def content(chunk: Any) -> str:
        if isinstance(chunk, dict):
            choices = chunk.get("choices", [])
            if not choices:
                return ""
            delta = choices[0].get("delta", {})
            return delta.get("content") or ""
        choices = getattr(chunk, "choices", [])
        if not choices:
            return ""
        delta = getattr(choices[0], "delta", None)
        return getattr(delta, "content", None) or ""


class _AnthropicAdapter:
    @staticmethod
    def is_stop(chunk: Any) -> bool:
        chunk_type = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", "")
        if chunk_type == "message_stop":
            return True
        if chunk_type == "message_delta":
            delta = (
                chunk.get("delta", {}) if isinstance(chunk, dict) else getattr(chunk, "delta", None)
            )
            stop_reason = (
                delta.get("stop_reason")
                if isinstance(delta, dict)
                else getattr(delta, "stop_reason", None)
            )
            return stop_reason is not None
        return False

    @staticmethod
    def content(chunk: Any) -> str:
        chunk_type = chunk.get("type") if isinstance(chunk, dict) else getattr(chunk, "type", "")
        if chunk_type != "content_block_delta":
            return ""
        if isinstance(chunk, dict):
            delta = chunk.get("delta", {})
            return delta.get("text") or delta.get("content") or ""
        delta = getattr(chunk, "delta", None)
        if delta is None:
            return ""
        return getattr(delta, "text", None) or getattr(delta, "content", None) or ""


_ADAPTERS: dict[str, type] = {
    "openai": _OpenAIAdapter,
    "anthropic": _AnthropicAdapter,
}


# ---------------------------------------------------------------------------
# StreamGuard
# ---------------------------------------------------------------------------


def _content_hash(text: str) -> str:
    return hashlib.md5(text.encode(), usedforsecurity=False).hexdigest()


def _try_active_session() -> Any:
    try:
        return _session_var.get()
    except LookupError:
        return None


class StreamGuard:
    """
    Context manager protecting async streams from cut-off and duplicate-chunk
    corruption.

    Args:
        format:         Built-in adapter — "openai" or "anthropic".
        stop_validator: Callable(chunk) -> bool. Escape hatch for any other
                        format. Takes precedence over format= if both given.
        deduplicate:    Drop chunks whose text content was already seen in
                        this stream. Default True.
        sequence_field: Field name to extract sequence number from each chunk
                        (e.g. ``"index"`` or ``"seq"``). If set, chunks with
                        regressing sequence values are flagged as out-of-order.
    """

    def __init__(
        self,
        format: str | None = None,
        stop_validator: Callable[[Any], bool] | None = None,
        deduplicate: bool = True,
        sequence_field: str | None = None,
    ) -> None:
        if format is not None and format not in _ADAPTERS:
            raise ValueError(
                f"Unknown format {format!r}. "
                f"Valid values: {list(_ADAPTERS)}. "
                "For other formats, pass stop_validator= instead."
            )
        self._adapter = _ADAPTERS.get(format) if format else None
        self._stop_validator = stop_validator
        self._deduplicate = deduplicate
        self._can_detect_stop = format is not None or stop_validator is not None
        self._sequence_field = sequence_field

        self._seen_hashes: set[str] = set()
        self._stop_seen = False
        self._chunk_count = 0
        self._duplicate_count = 0
        self._last_sequence: int | None = None
        self._out_of_order_count = 0
        self._audit: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def process(self, chunk: Any) -> Any | None:
        """
        Process one streaming chunk.

        Returns the chunk unchanged if it should be passed through,
        or None if it is a duplicate and should be dropped.
        """
        now = time.monotonic()

        # Stop-signal check runs before dedup so we never swallow a stop chunk.
        if self._can_detect_stop and self._is_stop(chunk):
            self._stop_seen = True
            self._audit.append(
                {
                    "event": "stream_stop",
                    "chunk_index": self._chunk_count,
                    "ts": now,
                }
            )
            self._log_to_session({"event": "stream_stop", "ts": now})

        content = self._extract_content(chunk)

        # Deduplication — only on chunks that carry text content.
        if self._deduplicate and content:
            h = _content_hash(content)
            if h in self._seen_hashes:
                self._duplicate_count += 1
                self._audit.append(
                    {
                        "event": "stream_duplicate",
                        "chunk_index": self._chunk_count,
                        "content_preview": content[:60],
                        "ts": now,
                    }
                )
                self._log_to_session(
                    {
                        "event": "stream_duplicate",
                        "content_preview": content[:60],
                        "ts": now,
                    }
                )
                return None
            self._seen_hashes.add(h)

        # Only count content-bearing chunks — stop-only chunks are metadata.
        if content:
            self._chunk_count += 1
            self._audit.append(
                {
                    "event": "stream_chunk",
                    "chunk_index": self._chunk_count,
                    "ts": now,
                }
            )

        # Sequence validation — detect out-of-order chunks.
        if self._sequence_field is not None:
            seq = self._extract_sequence(chunk)
            if seq is not None:
                if self._last_sequence is not None and seq < self._last_sequence:
                    self._out_of_order_count += 1
                    self._audit.append(
                        {
                            "event": "stream_out_of_order",
                            "chunk_index": self._chunk_count,
                            "expected_gte": self._last_sequence,
                            "got": seq,
                            "ts": now,
                        }
                    )
                    self._log_to_session(
                        {
                            "event": "stream_out_of_order",
                            "expected_gte": self._last_sequence,
                            "got": seq,
                            "ts": now,
                        }
                    )
                self._last_sequence = seq

        return chunk

    def audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit)

    @property
    def chunk_count(self) -> int:
        return self._chunk_count

    @property
    def duplicate_count(self) -> int:
        return self._duplicate_count

    @property
    def stop_seen(self) -> bool:
        return self._stop_seen

    @property
    def out_of_order_count(self) -> int:
        return self._out_of_order_count

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "StreamGuard":
        now = time.monotonic()
        self._audit.append({"event": "stream_start", "ts": now})
        self._log_to_session({"event": "stream_start", "tool": "StreamGuard", "ts": now})
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        now = time.monotonic()
        if exc_type is not None:
            # Stream raised — don't stack StreamCutOffError on top of it.
            return
        if self._can_detect_stop and not self._stop_seen:
            self._audit.append(
                {
                    "event": "stream_cutoff",
                    "chunks_received": self._chunk_count,
                    "ts": now,
                }
            )
            self._log_to_session(
                {
                    "event": "stream_cutoff",
                    "tool": "StreamGuard",
                    "chunks_received": self._chunk_count,
                    "ts": now,
                }
            )
            raise StreamCutOffError(
                f"Stream ended after {self._chunk_count} chunk(s) without a "
                f"stop signal. The response is likely incomplete."
            )
        self._audit.append(
            {
                "event": "stream_complete",
                "chunks_received": self._chunk_count,
                "duplicates_dropped": self._duplicate_count,
                "ts": now,
            }
        )
        self._log_to_session(
            {
                "event": "stream_complete",
                "tool": "StreamGuard",
                "chunks_received": self._chunk_count,
                "duplicates_dropped": self._duplicate_count,
                "ts": now,
            }
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_stop(self, chunk: Any) -> bool:
        if self._stop_validator is not None:
            return bool(self._stop_validator(chunk))
        if self._adapter is not None:
            return bool(self._adapter.is_stop(chunk))
        return False

    def _extract_content(self, chunk: Any) -> str:
        if self._adapter is not None:
            return self._adapter.content(chunk)
        return str(chunk)

    def _extract_sequence(self, chunk: Any) -> int | None:
        """Pull sequence number from *chunk* using *sequence_field*."""
        field = self._sequence_field
        if field is None:
            return None
        if isinstance(chunk, dict):
            val = chunk.get(field)
        else:
            val = getattr(chunk, field, None)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
        return None

    def _log_to_session(self, entry: dict[str, Any]) -> None:
        session = _try_active_session()
        if session is not None:
            session._audit.append(entry)
