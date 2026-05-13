"""
Tests for entity pattern validation (EntityPatternError).
"""

from __future__ import annotations

import pytest

from mycelium import EntityPatternError, Session, protect, protect_sync


@pytest.mark.asyncio
async def test_pattern_match_passes() -> None:
    """Entity matching the pattern should pass through."""
    @protect(entity_param="customer_id", entity_pattern=r"\bc\d+\b")
    async def fetch(customer_id: str) -> dict:
        return {"customer_id": customer_id}

    async with Session():
        result = await fetch(customer_id="c123")
    assert result["customer_id"] == "c123"


@pytest.mark.asyncio
async def test_pattern_mismatch_raises() -> None:
    """Entity not matching the pattern should raise EntityPatternError."""
    @protect(entity_param="customer_id", entity_pattern=r"\bc\d+\b")
    async def fetch(customer_id: str) -> dict:
        return {"customer_id": customer_id}

    async with Session():
        with pytest.raises(EntityPatternError) as exc_info:
            await fetch(customer_id="user_42")
    assert "c123" not in str(exc_info.value)
    assert "customer_id" in str(exc_info.value)


@pytest.mark.asyncio
async def test_pattern_with_critical_true() -> None:
    """entity_pattern works with critical=True too."""
    @protect(entity_param="email", entity_pattern=r".*@.*", critical=True)
    async def send(email: str) -> dict:
        return {"email": email}

    async with Session():
        with pytest.raises(EntityPatternError):
            await send(email="not-an-email")


@pytest.mark.asyncio
async def test_pattern_with_none_entity_id_skips_validation() -> None:
    """None entity_id should not trigger pattern validation."""
    @protect(entity_param="customer_id", entity_pattern=r"\bc\d+\b")
    async def fetch(customer_id: str | None = None) -> dict:
        return {"customer_id": customer_id}

    async with Session():
        result = await fetch(customer_id=None)
    assert result["customer_id"] is None


@pytest.mark.asyncio
async def test_no_entity_param_skips_validation() -> None:
    """Without entity_param, no pattern validation occurs."""
    @protect(entity_pattern=r"\bc\d+\b")
    async def fetch() -> dict:
        return {}

    async with Session():
        result = await fetch()
    assert result == {}


def test_sync_pattern_match_passes() -> None:
    """Entity pattern validation works with protect_sync."""
    @protect_sync(entity_param="customer_id", entity_pattern=r"\bc\d+\b")
    def fetch(customer_id: str) -> dict:
        return {"customer_id": customer_id}

    from mycelium.protect import _session_var
    token = _session_var.set(Session())
    try:
        result = fetch(customer_id="c123")
    finally:
        _session_var.reset(token)
    assert result["customer_id"] == "c123"


def test_sync_pattern_mismatch_raises() -> None:
    """Entity pattern mismatch raises with protect_sync."""
    @protect_sync(entity_param="customer_id", entity_pattern=r"\bc\d+\b")
    def fetch(customer_id: str) -> dict:
        return {"customer_id": customer_id}

    from mycelium.protect import _session_var
    token = _session_var.set(Session())
    try:
        with pytest.raises(EntityPatternError):
            fetch(customer_id="user_42")
    finally:
        _session_var.reset(token)


@pytest.mark.asyncio
async def test_email_pattern() -> None:
    """Email addresses should match @ pattern."""
    @protect(entity_param="email", entity_pattern=r".*@.*")
    async def lookup(email: str) -> dict:
        return {"email": email}

    async with Session():
        result = await lookup(email="alice@example.com")
    assert result["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_email_pattern_rejects_invalid() -> None:
    """Missing @ should raise for email pattern."""
    @protect(entity_param="email", entity_pattern=r".*@.*")
    async def lookup(email: str) -> dict:
        return {"email": email}

    async with Session():
        with pytest.raises(EntityPatternError):
            await lookup(email="not-an-email")


@pytest.mark.asyncio
async def test_uuid_pattern() -> None:
    """UUID format should match."""
    @protect(entity_param="uuid", entity_pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    async def fetch(uuid: str) -> dict:
        return {"uuid": uuid}

    async with Session():
        result = await fetch(uuid="550e8400-e29b-41d4-a716-446655440000")
    assert result["uuid"] == "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.asyncio
async def test_uuid_pattern_rejects_invalid() -> None:
    """Invalid UUID format should raise."""
    @protect(entity_param="uuid", entity_pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
    async def fetch(uuid: str) -> dict:
        return {"uuid": uuid}

    async with Session():
        with pytest.raises(EntityPatternError):
            await fetch(uuid="not-a-uuid")
