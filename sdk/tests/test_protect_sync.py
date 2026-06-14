import time

import pytest

from mycelium import protect_sync
from mycelium.cache import default_cache


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    default_cache._entries.clear()
    yield
    default_cache._entries.clear()


def test_cache_hit_avoids_second_call() -> None:
    calls = 0

    @protect_sync(entity_param="customer_id", ttl=60)
    def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        return {"customer_id": customer_id, "name": "Alice"}

    first = fetch_customer(customer_id="c1")
    second = fetch_customer(customer_id="c1")

    assert first == second
    assert calls == 1


def test_separate_cache_entries_per_entity() -> None:
    calls: list[str] = []

    @protect_sync(entity_param="customer_id", ttl=60)
    def fetch_customer(customer_id: str) -> dict:
        calls.append(customer_id)
        return {"customer_id": customer_id}

    fetch_customer(customer_id="alice")
    fetch_customer(customer_id="bob")
    fetch_customer(customer_id="alice")
    fetch_customer(customer_id="bob")

    assert calls == ["alice", "bob"]


def test_refetches_when_stale() -> None:
    calls = 0

    @protect_sync(entity_param="customer_id", ttl=0.05)
    def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        return {"customer_id": customer_id, "version": calls}

    first = fetch_customer(customer_id="c1")
    time.sleep(0.06)
    second = fetch_customer(customer_id="c1")

    assert first["version"] == 1
    assert second["version"] == 2
    assert calls == 2


def test_clears_cache_on_error() -> None:
    calls = 0

    @protect_sync(entity_param="customer_id", ttl=60)
    def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("upstream failure")
        return {"customer_id": customer_id}

    with pytest.raises(RuntimeError, match="upstream failure"):
        fetch_customer(customer_id="c1")

    result = fetch_customer(customer_id="c1")
    assert result == {"customer_id": "c1"}
    assert calls == 2


def test_shares_cache_with_protect() -> None:
    calls = 0

    @protect_sync(entity_param="customer_id", ttl=60)
    def fetch_customer_sync(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        return {"customer_id": customer_id, "source": "sync"}

    fetch_customer_sync(customer_id="c1")
    fetch_customer_sync(customer_id="c1")

    assert calls == 1
