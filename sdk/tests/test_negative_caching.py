"""
Tests for @protect(cache_empty=...) negative caching guard.

Scenarios:
  * cache_empty=0 → empty results never cached, always refetch
  * cache_empty=10 → empty results cached for 10s, non-empty for main TTL
  * Default (cache_empty=None) → empty results use normal TTL
  * Sync path works the same
  * Empty types: None, [], {}, ""
"""

from __future__ import annotations

import asyncio

import pytest

from mycelium import Session, protect, protect_sync


# ---------------------------------------------------------------------------
# cache_empty=0 — never cache empty results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_empty_zero_never_caches_empty_list() -> None:
    calls = [0]

    @protect(entity_param="q", cache_empty=0, ttl=60)
    async def search(q: str) -> list:
        calls[0] += 1
        return []  # empty result

    async with Session() as s:
        r1 = await search(q="nonexistent")
        r2 = await search(q="nonexistent")

    assert r1 == []
    assert r2 == []
    assert calls[0] == 2
    assert all(e["event"] == "cache_skip" for e in s.audit_log())


@pytest.mark.asyncio
async def test_cache_empty_zero_never_caches_none() -> None:
    calls = [0]

    @protect(entity_param="q", cache_empty=0, ttl=60)
    async def search(q: str) -> dict | None:
        calls[0] += 1
        return None

    async with Session() as s:
        await search(q="x")
        await search(q="x")

    assert calls[0] == 2


@pytest.mark.asyncio
async def test_cache_empty_zero_never_caches_empty_string() -> None:
    calls = [0]

    @protect(entity_param="q", cache_empty=0, ttl=60)
    async def search(q: str) -> str:
        calls[0] += 1
        return ""

    async with Session() as s:
        await search(q="x")
        await search(q="x")

    assert calls[0] == 2


@pytest.mark.asyncio
async def test_cache_empty_zero_never_caches_empty_dict() -> None:
    calls = [0]

    @protect(entity_param="q", cache_empty=0, ttl=60)
    async def search(q: str) -> dict:
        calls[0] += 1
        return {}

    async with Session() as s:
        await search(q="x")
        await search(q="x")

    assert calls[0] == 2


# ---------------------------------------------------------------------------
# cache_empty=0 — non-empty results still cached normally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_empty_zero_non_empty_still_cached() -> None:
    calls = [0]

    @protect(entity_param="q", cache_empty=0, ttl=60)
    async def search(q: str) -> list:
        calls[0] += 1
        return ["found"]

    async with Session() as s:
        r1 = await search(q="existing")
        r2 = await search(q="existing")

    assert r1 == r2 == ["found"]
    assert calls[0] == 1
    assert any(e["event"] == "cache_hit" for e in s.audit_log())


# ---------------------------------------------------------------------------
# cache_empty=short_ttl — empty cached briefly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_empty_short_caches_empty_briefly() -> None:
    calls = [0]

    @protect(entity_param="q", cache_empty=0.05, ttl=60)
    async def search(q: str) -> list:
        calls[0] += 1
        return []

    async with Session() as s:
        r1 = await search(q="x")
        await asyncio.sleep(0.01)
        r2 = await search(q="x")  # should be cache hit (within 0.05s)
        await asyncio.sleep(0.06)
        r3 = await search(q="x")  # should refetch (expired)

    assert r1 == r2 == r3 == []
    assert calls[0] == 2  # first + third
    assert any(e["event"] == "cache_hit" for e in s.audit_log())


# ---------------------------------------------------------------------------
# Default cache_empty=None — empty uses normal TTL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_cache_empty_uses_normal_ttl() -> None:
    calls = [0]

    @protect(entity_param="q", ttl=60)
    async def search(q: str) -> list:
        calls[0] += 1
        return []

    async with Session() as s:
        r1 = await search(q="x")
        r2 = await search(q="x")

    assert r1 == r2 == []
    assert calls[0] == 1
    assert any(e["event"] == "cache_hit" for e in s.audit_log())


# ---------------------------------------------------------------------------
# Sync protect_sync cache_empty
# ---------------------------------------------------------------------------


def test_sync_cache_empty_zero_never_caches_empty() -> None:
    from mycelium.protect import _session_var

    calls = [0]

    @protect_sync(entity_param="q", cache_empty=0, ttl=60)
    def search(q: str) -> list:
        calls[0] += 1
        return []

    session = Session()
    token = _session_var.set(session)
    try:
        search(q="x")
        search(q="x")
        assert calls[0] == 2
        assert all(e["event"] == "cache_skip" for e in session.audit_log())
    finally:
        _session_var.reset(token)


def test_sync_cache_empty_short_caches_empty_briefly() -> None:
    import time
    from mycelium.protect import _session_var

    calls = [0]

    @protect_sync(entity_param="q", cache_empty=0.05, ttl=60)
    def search(q: str) -> list:
        calls[0] += 1
        return []

    session = Session()
    token = _session_var.set(session)
    try:
        search(q="x")
        time.sleep(0.01)
        search(q="x")  # hit
        time.sleep(0.06)
        search(q="x")  # miss

        assert calls[0] == 2
    finally:
        _session_var.reset(token)
