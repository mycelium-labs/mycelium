from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from mycelium import FileStateFlushStorage, StateFlush, StateFlushError


def test_run_flushes_on_cancel() -> None:
    flush = StateFlush(flush_on=["cancel"])

    with pytest.raises(asyncio.CancelledError):
        with flush.run("thread-42", use_session=False) as run:
            run.record({"streamed": "partial answer"})
            raise asyncio.CancelledError()

    snapshot = flush.load("thread-42")
    assert snapshot is not None
    assert snapshot.status == "aborted"
    assert snapshot.reason == "cancel"
    assert snapshot.state["streamed"] == "partial answer"


def test_run_flushes_on_error() -> None:
    flush = StateFlush(flush_on=["error"])

    with pytest.raises(RuntimeError):
        with flush.run("thread-43", use_session=False) as run:
            run.record({"messages": [{"role": "user", "content": "hi"}]})
            raise RuntimeError("boom")

    snapshot = flush.load("thread-43")
    assert snapshot is not None
    assert snapshot.status == "error"
    assert snapshot.state["messages"][0]["content"] == "hi"


def test_resume_returns_flushed_state() -> None:
    flush = StateFlush()

    with flush.run("thread-44", use_session=False) as run:
        run.record({"streamed": "3.14159"})

    assert flush.resume("thread-44") == {"streamed": "3.14159"}


def test_resume_raises_when_missing() -> None:
    flush = StateFlush()
    with pytest.raises(StateFlushError):
        flush.resume("missing")


def test_disconnect_flushes_when_configured() -> None:
    flush = StateFlush(flush_on=["disconnect"])

    with flush.run("thread-45", use_session=False) as run:
        run.record({"streamed": "visible chunk"})
        run.disconnect()

    snapshot = flush.load("thread-45")
    assert snapshot is not None
    assert snapshot.status == "aborted"
    assert snapshot.reason == "disconnect"


def test_file_storage_survives_new_instance() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "state.json"
        flush = StateFlush(storage=FileStateFlushStorage(path), flush_on=["cancel"])

        with pytest.raises(asyncio.CancelledError):
            with flush.run("thread-46", use_session=False) as run:
                run.record({"streamed": "persisted"})
                raise asyncio.CancelledError()

        restored = StateFlush(storage=FileStateFlushStorage(path))
        assert restored.resume("thread-46") == {"streamed": "persisted"}


def test_run_integrates_session() -> None:
    from mycelium import protect_sync

    flush = StateFlush()
    calls: list[str] = []

    @protect_sync(entity_param="customer_id", ttl=60)
    def fetch_customer(customer_id: str) -> dict[str, str]:
        calls.append(customer_id)
        return {"customer_id": customer_id}

    with flush.run("thread-47", use_session=True) as run:
        run.record({"step": 1})
        fetch_customer(customer_id="c1")
        fetch_customer(customer_id="c1")

    assert len(calls) == 1
    assert flush.resume("thread-47") == {"step": 1}
