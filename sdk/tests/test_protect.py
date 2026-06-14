import asyncio

import pytest

from mycelium import protect
from mycelium.cache import default_cache


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    default_cache._entries.clear()
    yield
    default_cache._entries.clear()


async def test_cache_hit_avoids_second_call() -> None:
    calls = 0

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        return {"customer_id": customer_id, "name": "Alice"}

    first = await fetch_customer(customer_id="c1")
    second = await fetch_customer(customer_id="c1")

    assert first == second
    assert calls == 1


async def test_separate_cache_entries_per_entity() -> None:
    calls: list[str] = []

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        calls.append(customer_id)
        return {"customer_id": customer_id}

    await fetch_customer(customer_id="alice")
    await fetch_customer(customer_id="bob")
    await fetch_customer(customer_id="alice")
    await fetch_customer(customer_id="bob")

    assert calls == ["alice", "bob"]


async def test_refetches_when_stale() -> None:
    calls = 0

    @protect(entity_param="customer_id", ttl=0.05)
    async def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        return {"customer_id": customer_id, "version": calls}

    first = await fetch_customer(customer_id="c1")
    await asyncio.sleep(0.06)
    second = await fetch_customer(customer_id="c1")

    assert first["version"] == 1
    assert second["version"] == 2
    assert calls == 2


async def test_clears_cache_on_error() -> None:
    calls = 0

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("upstream failure")
        return {"customer_id": customer_id}

    with pytest.raises(RuntimeError, match="upstream failure"):
        await fetch_customer(customer_id="c1")

    result = await fetch_customer(customer_id="c1")
    assert result == {"customer_id": "c1"}
    assert calls == 2


async def test_no_entity_param_caches_per_tool() -> None:
    calls = 0

    @protect(ttl=60)
    async def fetch_status() -> dict:
        nonlocal calls
        calls += 1
        return {"status": "ok"}

    await fetch_status()
    await fetch_status()

    assert calls == 1
