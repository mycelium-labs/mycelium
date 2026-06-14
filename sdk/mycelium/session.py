"""Per-run cache isolation via ContextVar."""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any

from mycelium.cache import ToolCache, default_cache

_session_var: ContextVar[Session | None] = ContextVar("mycelium_session", default=None)


def get_cache() -> ToolCache:
    session = _session_var.get()
    if session is not None:
        return session._cache
    return default_cache


class Session:
    """
    Per-run cache scope. Create one per agent run to prevent cross-run leakage.

        async with Session():
            result = await fetch_customer(customer_id="c1")

    Sync tools (CrewAI, Smolagents):

        with Session():
            result = fetch_customer(customer_id="c1")
    """

    def __init__(self) -> None:
        self._cache = ToolCache()
        self._token: Token[Session | None] | None = None

    def __enter__(self) -> Session:
        self._token = _session_var.set(self)
        return self

    def __exit__(self, *_: Any) -> None:
        if self._token is not None:
            _session_var.reset(self._token)
            self._token = None

    async def __aenter__(self) -> Session:
        return self.__enter__()

    async def __aexit__(self, *_: Any) -> None:
        self.__exit__()
