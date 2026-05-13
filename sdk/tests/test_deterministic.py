"""
Tests for @protect(deterministic=False) and auto variance detection.

Scenarios:
  * deterministic=False → never caches, always calls through
  * deterministic=False + entity_field still validates tenancy
  * Auto variance warning when same-args calls return different values
  * deterministic=True (default) with stable tool → caches normally
  * protect_sync deterministic=False
"""

from __future__ import annotations

import pytest

from mycelium import Session, protect, protect_sync

# ---------------------------------------------------------------------------
# deterministic=False — no caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_false_never_caches() -> None:
    calls = [0]

    @protect(entity_param="symbol", deterministic=False)
    async def get_stock_price(symbol: str) -> dict:
        calls[0] += 1
        return {"symbol": symbol, "price": 100 + calls[0]}

    async with Session() as s:
        r1 = await get_stock_price(symbol="AAPL")
        r2 = await get_stock_price(symbol="AAPL")
        r3 = await get_stock_price(symbol="AAPL")

    assert r1["price"] == 101
    assert r2["price"] == 102
    assert r3["price"] == 103
    assert calls[0] == 3
    assert all(e["event"] == "cache_skip" for e in s.audit_log())


@pytest.mark.asyncio
async def test_deterministic_false_logs_cache_skip() -> None:
    @protect(deterministic=False)
    async def roll_dice() -> int:
        return 4  # chosen by fair dice roll

    async with Session() as s:
        await roll_dice()

    skips = [e for e in s.audit_log() if e["event"] == "cache_skip"]
    assert len(skips) == 1
    assert skips[0]["reason"] == "non_deterministic"


@pytest.mark.asyncio
async def test_deterministic_false_still_validates_entity_field() -> None:
    @protect(entity_param="id", entity_field="id", deterministic=False)
    async def fetch(id: str) -> dict:
        return {"id": "wrong"}

    async with Session():
        with pytest.raises(Exception):  # TenancyMismatchError
            await fetch(id="right")


# ---------------------------------------------------------------------------
# deterministic=True (default) — normal caching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deterministic_true_caches_normally() -> None:
    calls = [0]

    @protect(entity_param="symbol", ttl=60)
    async def get_price(symbol: str) -> dict:
        calls[0] += 1
        return {"symbol": symbol, "price": 150}

    async with Session() as s:
        r1 = await get_price(symbol="GOOG")
        r2 = await get_price(symbol="GOOG")

    assert r1 == r2
    assert calls[0] == 1
    assert any(e["event"] == "cache_hit" for e in s.audit_log())


# ---------------------------------------------------------------------------
# Variance auto-detection warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_variance_warning_on_non_deterministic_tool() -> None:
    """When a deterministic=True tool returns different values for the same args
    within a short window, a variance_warning is logged."""
    import asyncio

    calls = [0]

    @protect(entity_param="symbol", ttl=0.01)
    async def get_price(symbol: str) -> dict:
        calls[0] += 1
        return {"symbol": symbol, "price": 100 + calls[0]}

    async with Session() as s:
        for _ in range(5):
            await get_price(symbol="AAPL")
            await asyncio.sleep(0.02)  # force TTL expiry so next call refetches

    warnings = [e for e in s.audit_log() if e["event"] == "variance_warning"]
    assert len(warnings) >= 1
    assert warnings[-1]["tool"] == "get_price"
    assert warnings[-1]["unique_values"] >= 3


@pytest.mark.asyncio
async def test_no_variance_warning_on_stable_tool() -> None:
    """A truly deterministic tool produces no variance warnings."""

    @protect(entity_param="symbol", ttl=60)
    async def get_price(symbol: str) -> dict:
        return {"symbol": symbol, "price": 150}

    async with Session() as s:
        for _ in range(5):
            await get_price(symbol="AAPL")

    warnings = [e for e in s.audit_log() if e["event"] == "variance_warning"]
    assert len(warnings) == 0


# ---------------------------------------------------------------------------
# Sync protect_sync deterministic=False
# ---------------------------------------------------------------------------


def test_sync_deterministic_false_never_caches() -> None:
    from mycelium.protect import _session_var

    calls = [0]

    @protect_sync(entity_param="symbol", deterministic=False)
    def get_price(symbol: str) -> dict:
        calls[0] += 1
        return {"symbol": symbol, "price": 100 + calls[0]}

    session = Session()
    token = _session_var.set(session)
    try:
        r1 = get_price(symbol="AAPL")
        r2 = get_price(symbol="AAPL")

        assert r1["price"] == 101
        assert r2["price"] == 102
        assert calls[0] == 2
        assert all(e["event"] == "cache_skip" for e in session.audit_log())
    finally:
        _session_var.reset(token)


def test_sync_variance_warning_on_non_deterministic_tool() -> None:
    import time

    from mycelium.protect import _session_var

    calls = [0]

    @protect_sync(entity_param="symbol", ttl=0.01)
    def get_price(symbol: str) -> dict:
        calls[0] += 1
        return {"symbol": symbol, "price": 100 + calls[0]}

    session = Session()
    token = _session_var.set(session)
    try:
        for _ in range(5):
            get_price(symbol="AAPL")
            time.sleep(0.02)  # force TTL expiry so next call refetches

        warnings = [e for e in session.audit_log() if e["event"] == "variance_warning"]
        assert len(warnings) >= 1
    finally:
        _session_var.reset(token)
