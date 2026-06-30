"""POSIX advisory file locking for JSON-backed storage."""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import IO


class PathFileLock:
    """Exclusive lock on a sidecar ``.lock`` file next to the data path."""

    def __init__(self, data_path: str | Path) -> None:
        self._data_path = Path(data_path)
        self._lock_path = self._data_path.with_suffix(self._data_path.suffix + ".lock")
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def acquire(self) -> Generator[None, None, None]:
        with self._lock_path.open("a+", encoding="utf-8") as handle:
            _lock_exclusive(handle)
            try:
                yield
            finally:
                _unlock(handle)


def _lock_exclusive(handle: IO[str]) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock(handle: IO[str]) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
