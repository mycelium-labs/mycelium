"""
Tests for @protect(entity_field=...) round-trip tenancy validation.

Scenarios:
  * Response contains wrong entity_id → TenancyMismatchError, cache cleared
  * Response contains correct entity_id → passes through normally
  * entity_field on object (not just dict)
  * critical=True path also validates
  * protect_sync path also validates
  * No entity_field → no extra validation (backward compatible)
"""

from __future__ import annotations

import pytest

from mycelium import Session, TenancyMismatchError, protect, protect_sync


# ---------------------------------------------------------------------------
# Async @protect with entity_field
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entity_field_mismatch_raises_and_clears_cache() -> None:
    """If the response has the wrong customer_id, TenancyMismatchError is raised
    and the cache entry is cleared so the next call retries."""
    calls = [0]

    @protect(entity_param="customer_id", entity_field="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        calls[0] += 1
        # Bug: returns data for the wrong customer
        return {"customer_id": "c2", "email": "bob@example.com"}

    async with Session() as s:
        with pytest.raises(TenancyMismatchError) as exc_info:
            await fetch_customer(customer_id="c1")

    assert exc_info.value.expected == "c1"
    assert exc_info.value.actual == "c2"
    assert exc_info.value.field == "customer_id"
    assert any(
        e["event"] == "cache_error" and "tenancy_mismatch" in e.get("error", "")
        for e in s.audit_log()
    )


@pytest.mark.asyncio
async def test_entity_field_match_passes_through() -> None:
    """Correct round-trip: request customer_id matches response customer_id."""

    @protect(entity_param="customer_id", entity_field="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return {"customer_id": customer_id, "email": "alice@example.com"}

    async with Session() as s:
        result = await fetch_customer(customer_id="c1")

    assert result["customer_id"] == "c1"
    assert any(e["event"] == "cache_add" for e in s.audit_log())


@pytest.mark.asyncio
async def test_entity_field_on_object_not_dict() -> None:
    """entity_field works on objects with attributes, not just dicts."""

    class Customer:
        def __init__(self, customer_id: str) -> None:
            self.customer_id = customer_id

    @protect(entity_param="customer_id", entity_field="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> Customer:
        return Customer(customer_id=customer_id)

    async with Session() as s:
        result = await fetch_customer(customer_id="c1")

    assert result.customer_id == "c1"
    assert any(e["event"] == "cache_add" for e in s.audit_log())


@pytest.mark.asyncio
async def test_entity_field_mismatch_on_object() -> None:
    """Tenancy mismatch on object attributes also raises."""

    class Customer:
        def __init__(self, customer_id: str) -> None:
            self.customer_id = customer_id

    @protect(entity_param="customer_id", entity_field="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> Customer:
        return Customer(customer_id="wrong")

    async with Session() as s:
        with pytest.raises(TenancyMismatchError):
            await fetch_customer(customer_id="c1")

    assert any(
        e["event"] == "cache_error" and "tenancy_mismatch" in e.get("error", "")
        for e in s.audit_log()
    )


@pytest.mark.asyncio
async def test_critical_true_also_validates_entity_field() -> None:
    """critical=True bypasses cache but still runs entity_field validation."""

    @protect(entity_param="customer_id", entity_field="customer_id", critical=True)
    async def fetch_customer(customer_id: str) -> dict:
        return {"customer_id": "wrong"}

    with pytest.raises(TenancyMismatchError):
        await fetch_customer(customer_id="c1")


@pytest.mark.asyncio
async def test_no_entity_field_backward_compatible() -> None:
    """Without entity_field, wrong response customer_id is silently accepted
    (backward compatible — no validation)."""

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        return {"customer_id": "c2"}

    async with Session() as s:
        result = await fetch_customer(customer_id="c1")

    assert result["customer_id"] == "c2"
    assert any(e["event"] == "cache_add" for e in s.audit_log())


# ---------------------------------------------------------------------------
# Sync protect_sync with entity_field
# ---------------------------------------------------------------------------


def test_sync_entity_field_mismatch_raises_and_clears_cache() -> None:
    from mycelium.protect import _session_var

    calls = [0]

    @protect_sync(entity_param="customer_id", entity_field="customer_id", ttl=60)
    def fetch_customer(customer_id: str) -> dict:
        calls[0] += 1
        return {"customer_id": "c2"}

    session = Session()
    token = _session_var.set(session)
    try:
        with pytest.raises(TenancyMismatchError) as exc_info:
            fetch_customer(customer_id="c1")

        assert exc_info.value.expected == "c1"
        assert exc_info.value.actual == "c2"
        assert any(
            e["event"] == "cache_error" and "tenancy_mismatch" in e.get("error", "")
            for e in session.audit_log()
        )
    finally:
        _session_var.reset(token)


def test_sync_entity_field_match_passes_through() -> None:
    from mycelium.protect import _session_var

    @protect_sync(entity_param="customer_id", entity_field="customer_id", ttl=60)
    def fetch_customer(customer_id: str) -> dict:
        return {"customer_id": customer_id}

    session = Session()
    token = _session_var.set(session)
    try:
        result = fetch_customer(customer_id="c1")
        assert result["customer_id"] == "c1"
        assert any(e["event"] == "cache_add" for e in session.audit_log())
    finally:
        _session_var.reset(token)


def test_sync_critical_true_validates_entity_field() -> None:
    from mycelium.protect import _session_var

    @protect_sync(entity_param="customer_id", entity_field="customer_id", critical=True)
    def fetch_customer(customer_id: str) -> dict:
        return {"customer_id": "wrong"}

    session = Session()
    token = _session_var.set(session)
    try:
        with pytest.raises(TenancyMismatchError):
            fetch_customer(customer_id="c1")
    finally:
        _session_var.reset(token)


# ---------------------------------------------------------------------------
# Cache behaviour after mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mismatch_then_retry_with_correct_response_succeeds() -> None:
    """After a tenancy mismatch clears the cache, a subsequent call with the
    correct response succeeds and is cached."""
    calls = [0]

    @protect(entity_param="customer_id", entity_field="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        calls[0] += 1
        if calls[0] == 1:
            return {"customer_id": "c2"}  # wrong on first call
        return {"customer_id": customer_id}  # correct on retry

    async with Session() as s:
        with pytest.raises(TenancyMismatchError):
            await fetch_customer(customer_id="c1")
        result = await fetch_customer(customer_id="c1")

    assert result["customer_id"] == "c1"
    assert calls[0] == 2
    assert any(e["event"] == "cache_add" for e in s.audit_log())
