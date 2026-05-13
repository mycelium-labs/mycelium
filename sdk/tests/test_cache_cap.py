"""
Tests for Session max_entries (hard cache cap / LRU eviction).
"""

from __future__ import annotations

import pytest

from mycelium import Session, protect


@pytest.mark.asyncio
async def test_max_entries_none_no_eviction() -> None:
    """Default Session (max_entries=None) never evicts."""
    s = Session()

    @protect(entity_param="uid", ttl=60)
    async def get(uid: str) -> str:
        return uid

    async with s:
        for i in range(5):
            await get(uid=f"u{i}")

    assert s.cache_size() == 5


@pytest.mark.asyncio
async def test_max_entries_evicts_oldest_on_add() -> None:
    """With max_entries=3, adding a 4th entry evicts the LRU."""
    s = Session(max_entries=3)

    @protect(entity_param="uid", ttl=60)
    async def get(uid: str) -> str:
        return uid

    async with s:
        await get(uid="u1")
        await get(uid="u2")
        await get(uid="u3")
        # u1 is LRU
        await get(uid="u4")

    assert s.cache_size() == 3
    # u1 should have been evicted
    assert not any(e["event"] == "cache_hit" and e.get("entity_id") == "u1" for e in s.audit_log())
    assert any(e["event"] == "cache_evict_lru" for e in s.audit_log())


@pytest.mark.asyncio
async def test_max_entries_hit_refreshes_lru() -> None:
    """Accessing u1 refreshes it; u2 becomes LRU and gets evicted."""
    s = Session(max_entries=3)

    @protect(entity_param="uid", ttl=60)
    async def get(uid: str) -> str:
        return uid

    async with s:
        await get(uid="u1")
        await get(uid="u2")
        await get(uid="u3")
        # Refresh u1 — now u2 is LRU
        await get(uid="u1")
        await get(uid="u4")

    assert s.cache_size() == 3
    # u2 should have been evicted, not u1
    audit = s.audit_log()
    evict_event = [e for e in audit if e["event"] == "cache_evict_lru"][0]
    assert evict_event["entity_id"] == "u2"


@pytest.mark.asyncio
async def test_max_entries_does_not_evict_expired_entries() -> None:
    """Expired entries don't count toward max_entries; only live ones."""
    s = Session(max_entries=2)

    @protect(entity_param="uid", ttl=0.05)
    async def get(uid: str) -> str:
        return uid

    async with s:
        await get(uid="u1")
        await get(uid="u2")
        import asyncio

        await asyncio.sleep(0.11)
        # Both expired; adding u3 should not trigger eviction
        await get(uid="u3")

    assert s.cache_size() == 1
    assert not any(e["event"] == "cache_evict_lru" for e in s.audit_log())


@pytest.mark.asyncio
async def test_max_entries_exactly_at_limit() -> None:
    """Adding up to max_entries should not evict anything."""
    s = Session(max_entries=2)

    @protect(entity_param="uid", ttl=60)
    async def get(uid: str) -> str:
        return uid

    async with s:
        await get(uid="u1")
        await get(uid="u2")

    assert s.cache_size() == 2
    assert not any(e["event"] == "cache_evict_lru" for e in s.audit_log())


@pytest.mark.asyncio
async def test_max_entries_sync_path() -> None:
    """LRU eviction works with protect_sync too."""
    from mycelium import protect_sync

    s = Session(max_entries=2)

    @protect_sync(entity_param="uid", ttl=60)
    def get(uid: str) -> str:
        return uid

    from mycelium.protect import _session_var

    token = _session_var.set(s)
    try:
        get(uid="u1")
        get(uid="u2")
        get(uid="u3")
    finally:
        _session_var.reset(token)

    assert s.cache_size() == 2
    assert any(e["event"] == "cache_evict_lru" for e in s.audit_log())
