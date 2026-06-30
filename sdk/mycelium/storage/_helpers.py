"""Shared helpers for durable ledger storage backends."""

from __future__ import annotations

import os
from typing import Any, Literal, Protocol, TypeVar

ClaimOutcome = Literal["claimed", "completed", "in_flight"]

E = TypeVar("E")


class EntryProtocol(Protocol):
    request_id: str
    status: str

    def to_dict(self) -> dict[str, Any]: ...


def default_try_claim_inflight(
    storage: Any,
    entry: E,
) -> tuple[ClaimOutcome, E | None]:
    """Non-atomic claim used by memory storage; file storage wraps this in a lock."""
    existing = storage.get(entry.request_id)
    if existing is not None:
        if existing.status == "completed":
            return "completed", existing
        if existing.status == "in-flight":
            return "in_flight", existing
    storage.set(entry)
    return "claimed", None


def resolve_storage_url(raw: dict[str, Any], *, url_key: str = "url") -> str:
    """Resolve a connection string from config or an environment variable."""
    if url_key in raw:
        return str(raw[url_key])
    env_key = raw.get(f"{url_key}_env")
    if env_key:
        value = os.environ.get(str(env_key))
        if not value:
            raise ValueError(f"environment variable {env_key!r} is not set")
        return value
    raise ValueError(f"storage requires {url_key!r} or {url_key}_env")
