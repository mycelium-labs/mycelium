"""
Tests for StreamGuard out-of-order stream chunk detection.
"""

from __future__ import annotations

import pytest

from mycelium import StreamGuard


@pytest.mark.asyncio
async def test_ordered_sequence_passes() -> None:
    """Chunks with monotonically increasing sequence pass through."""
    chunks = [
        {"index": 0, "content": "Hello"},
        {"index": 1, "content": " world"},
        {"index": 2, "content": "!"},
    ]

    async with StreamGuard(stop_validator=lambda c: True, sequence_field="index") as guard:
        for c in chunks:
            result = guard.process(c)
            assert result is c

    assert guard.out_of_order_count == 0


@pytest.mark.asyncio
async def test_out_of_order_detected() -> None:
    """A chunk with regressing sequence is flagged."""
    chunks = [
        {"index": 0, "content": "Hello"},
        {"index": 2, "content": " world"},
        {"index": 1, "content": "!"},  # out of order
    ]

    async with StreamGuard(stop_validator=lambda c: True, sequence_field="index") as guard:
        for c in chunks:
            guard.process(c)

    assert guard.out_of_order_count == 1
    assert any(e["event"] == "stream_out_of_order" for e in guard.audit_log())
    ooo_events = [e for e in guard.audit_log() if e["event"] == "stream_out_of_order"]
    assert ooo_events[0]["got"] == 1
    assert ooo_events[0]["expected_gte"] == 2


@pytest.mark.asyncio
async def test_multiple_out_of_order() -> None:
    """Multiple out-of-order chunks are all detected."""
    chunks = [
        {"index": 5, "content": "a"},
        {"index": 3, "content": "b"},  # ooo
        {"index": 4, "content": "c"},  # ooo (less than 5 but >= 3? actually 4 >= 3 so it's ok)
        {"index": 1, "content": "d"},  # ooo
    ]

    async with StreamGuard(stop_validator=lambda c: True, sequence_field="index") as guard:
        for c in chunks:
            guard.process(c)

    assert guard.out_of_order_count == 2
    assert any(e["event"] == "stream_out_of_order" for e in guard.audit_log())


@pytest.mark.asyncio
async def test_no_sequence_field_no_check() -> None:
    """Without sequence_field, no sequence check is performed."""
    chunks = [
        {"content": "Hello"},
        {"content": " world"},
    ]

    async with StreamGuard(stop_validator=lambda c: True) as guard:
        for c in chunks:
            guard.process(c)

    assert guard.out_of_order_count == 0
    assert not any(e["event"] == "stream_out_of_order" for e in guard.audit_log())


@pytest.mark.asyncio
async def test_sequence_with_object_chunks() -> None:
    """Sequence detection works with object-style chunks too."""
    class FakeChunk:
        def __init__(self, seq: int, text: str):
            self.seq = seq
            self.content = text

    chunks = [
        FakeChunk(0, "Hello"),
        FakeChunk(2, " world"),
        FakeChunk(1, "!"),  # ooo
    ]

    async with StreamGuard(stop_validator=lambda c: True, sequence_field="seq") as guard:
        for c in chunks:
            guard.process(c)

    assert guard.out_of_order_count == 1


@pytest.mark.asyncio
async def test_sequence_with_missing_field() -> None:
    """Chunks without the sequence field don't trigger false positives."""
    chunks = [
        {"index": 0, "content": "Hello"},
        {"content": " world"},  # no index field
        {"index": 2, "content": "!"},
    ]

    async with StreamGuard(stop_validator=lambda c: True, sequence_field="index") as guard:
        for c in chunks:
            guard.process(c)

    assert guard.out_of_order_count == 0
