import asyncio

import pytest

from mycelium import Session, protect, protect_sync
from mycelium.cache import default_cache


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    default_cache._entries.clear()
    yield
    default_cache._entries.clear()


async def test_separate_sessions_do_not_share_cache() -> None:
    calls = 0

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        return {"customer_id": customer_id}

    async with Session():
        await fetch_customer(customer_id="c1")
        await fetch_customer(customer_id="c1")

    async with Session():
        await fetch_customer(customer_id="c1")

    assert calls == 2


async def test_concurrent_sessions_are_isolated() -> None:
    calls: list[str] = []

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        calls.append(customer_id)
        return {"customer_id": customer_id}

    async def run(customer_id: str) -> None:
        async with Session():
            await fetch_customer(customer_id=customer_id)
            await fetch_customer(customer_id=customer_id)

    await asyncio.gather(run("alice"), run("bob"))

    assert sorted(calls) == ["alice", "bob"]


async def test_session_cache_not_visible_outside_context() -> None:
    calls = 0

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        return {"customer_id": customer_id}

    async with Session():
        await fetch_customer(customer_id="c1")

    await fetch_customer(customer_id="c1")

    assert calls == 2


def test_sync_session_isolates_protect_sync() -> None:
    calls = 0

    @protect_sync(entity_param="customer_id", ttl=60)
    def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        return {"customer_id": customer_id}

    with Session():
        fetch_customer(customer_id="c1")
        fetch_customer(customer_id="c1")

    with Session():
        fetch_customer(customer_id="c1")

    assert calls == 2
