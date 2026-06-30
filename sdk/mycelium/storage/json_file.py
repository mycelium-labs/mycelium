"""Locked read-modify-write helpers for JSON dict files."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from mycelium.storage.file_lock import PathFileLock

T = TypeVar("T")


class LockedJsonDictFile:
    """JSON object file with exclusive ``fcntl`` locking on read-modify-write."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = PathFileLock(self._path)

    def load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        return data

    def save(self, data: dict[str, dict[str, Any]]) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, default=str)
        os.replace(tmp, self._path)

    def read_modify_write(
        self,
        fn: Callable[[dict[str, dict[str, Any]]], T],
    ) -> T:
        with self._lock.acquire():
            data = self.load()
            result = fn(data)
            self.save(data)
            return result

    def read_modify_write_no_save(
        self,
        fn: Callable[[dict[str, dict[str, Any]]], T],
    ) -> T:
        """Run ``fn`` under lock without persisting changes (read-only or external save)."""
        with self._lock.acquire():
            data = self.load()
            return fn(data)
