"""TTL cache used by @protect and Session."""

from __future__ import annotations

import time
from typing import Any


class _CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, expires_at: float) -> None:
        self.value = value
        self.expires_at = expires_at


class ToolCache:
    """In-memory TTL cache keyed by tool name and optional entity id."""

    def __init__(self) -> None:
        self._entries: dict[str, _CacheEntry] = {}

    @staticmethod
    def _key(tool_name: str, entity_id: Any | None) -> str:
        if entity_id is None:
            return tool_name
        return f"{tool_name}:{entity_id}"

    def get(self, tool_name: str, entity_id: Any | None) -> Any | None:
        key = self._key(tool_name, entity_id)
        entry = self._entries.get(key)
        if entry is None:
            return None

        if time.monotonic() >= entry.expires_at:
            self._entries.pop(key, None)
            return None

        return entry.value

    def set(self, tool_name: str, entity_id: Any | None, value: Any, ttl: float) -> None:
        key = self._key(tool_name, entity_id)
        self._entries[key] = _CacheEntry(value, time.monotonic() + ttl)

    def clear(self, tool_name: str, entity_id: Any | None) -> None:
        self._entries.pop(self._key(tool_name, entity_id), None)


default_cache = ToolCache()
