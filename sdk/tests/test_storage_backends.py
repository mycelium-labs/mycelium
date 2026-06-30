"""Tests for Redis, Postgres, and file-locked ledger storage backends."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from mycelium import (
    ActionLedger,
    FileLedgerStorage,
    LedgerEntry,
    LedgerPendingError,
    RedisLedgerStorage,
)


def _entry(request_id: str, *, status: str = "in-flight") -> LedgerEntry:
    return LedgerEntry(
        request_id=request_id,
        tool="send_payment",
        args=[],
        kwargs={"amount": 10},
        status=status,
    )


def test_file_storage_serializes_concurrent_claims(tmp_path: Path) -> None:
    storage = FileLedgerStorage(tmp_path / "ledger.json")
    ledger = ActionLedger(storage=storage)
    barrier = threading.Barrier(2)
    results: list[str] = []

    def claim() -> None:
        barrier.wait()
        try:
            ledger.claim("req-1", "send_payment", (), {"amount": 10})
            results.append("claimed")
        except LedgerPendingError:
            results.append("pending")

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == ["claimed", "pending"]
    assert ledger.get("req-1") is not None
    assert ledger.get("req-1").status == "in-flight"


def test_redis_storage_atomic_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    fakeredis = pytest.importorskip("fakeredis")
    fake = fakeredis.FakeRedis(decode_responses=True)

    def from_url(url: str, **kwargs: object) -> object:
        return fake

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", from_url)

    storage = RedisLedgerStorage("redis://test")
    ledger = ActionLedger(storage=storage)

    first = ledger.claim("req-redis", "send_payment", (), {"amount": 1})
    assert first.status == "in-flight"

    with pytest.raises(LedgerPendingError):
        ledger.claim("req-redis", "send_payment", (), {"amount": 1})

    completed = ledger.complete("req-redis", {"ok": True})
    assert completed.status == "completed"

    replay = ledger.claim("req-redis", "send_payment", (), {"amount": 1})
    assert replay.status == "completed"
    assert replay.result == {"ok": True}


def test_redis_storage_retries_after_failed_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    fakeredis = pytest.importorskip("fakeredis")
    fake = fakeredis.FakeRedis(decode_responses=True)

    def from_url(url: str, **kwargs: object) -> object:
        return fake

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", from_url)

    storage = RedisLedgerStorage("redis://test")
    ledger = ActionLedger(storage=storage)
    ledger.claim("req-fail", "send_payment", (), {})
    ledger.fail("req-fail", RuntimeError("boom"))

    retry = ledger.claim("req-fail", "send_payment", (), {})
    assert retry.status == "in-flight"


@pytest.mark.skipif(
    not os.environ.get("MYCELIUM_TEST_POSTGRES_DSN"),
    reason="set MYCELIUM_TEST_POSTGRES_DSN to run Postgres integration tests",
)
def test_postgres_storage_atomic_claim() -> None:
    from mycelium import PostgresLedgerStorage

    dsn = os.environ["MYCELIUM_TEST_POSTGRES_DSN"]
    storage = PostgresLedgerStorage(dsn, table="mycelium_test_action_ledger")
    ledger = ActionLedger(storage=storage)

    request_id = "req-pg-integration"
    entry = ledger.get(request_id)
    if entry is not None and entry.status == "in-flight":
        ledger.fail(request_id, RuntimeError("cleanup stale in-flight row"))

    first = ledger.claim(request_id, "send_payment", (), {"amount": 99})
    assert first.status == "in-flight"

    with pytest.raises(LedgerPendingError):
        ledger.claim(request_id, "send_payment", (), {"amount": 99})

    ledger.complete(request_id, {"paid": True})
    replay = ledger.claim(request_id, "send_payment", (), {"amount": 99})
    assert replay.status == "completed"
    assert replay.result == {"paid": True}


def test_config_builds_redis_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("fakeredis")
    from mycelium.config import MyceliumConfig

    monkeypatch.setenv("MYCELIUM_REDIS_URL", "redis://localhost:6379/0")
    storage = MyceliumConfig._build_ledger_storage(
        {
            "storage": "redis",
            "url_env": "MYCELIUM_REDIS_URL",
            "prefix": "test:action:",
        }
    )
    assert isinstance(storage, RedisLedgerStorage)


def test_config_builds_postgres_storage() -> None:
    pytest.importorskip("psycopg")
    from mycelium.config import MyceliumConfig
    from mycelium.storage.postgres_ledger import PostgresLedgerStorage

    storage = MyceliumConfig._build_ledger_storage(
        {
            "storage": "postgres",
            "dsn": "postgresql://example",
            "table": "mycelium_action_ledger",
        }
    )
    assert isinstance(storage, PostgresLedgerStorage)
