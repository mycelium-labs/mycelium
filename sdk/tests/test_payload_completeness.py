"""
Tests for payload completeness detection via mycelium.http transport wrapper.

Scenarios:
  * Content-Length mismatch (server promises more than it sends)
  * Truncated JSON (unclosed braces, strings, arrays)
  * Empty JSON body
  * Valid JSON passes through untouched
  * @protect integration: PayloadIncompleteError clears cache + logs cache_error
  * Sync and async clients both covered
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from mycelium import Session, protect, protect_sync
from mycelium.http import (
    AsyncClient,
    Client,
    PayloadIncompleteError,
    _guard_response,
    _is_json_complete,
)


# ---------------------------------------------------------------------------
# Helpers: mock httpx.Response
# ---------------------------------------------------------------------------


def _mock_response(
    content: bytes,
    headers: dict[str, str] | None = None,
    status_code: int = 200,
) -> httpx.Response:
    """Build a real-ish httpx.Response for the guard functions."""
    request = httpx.Request("GET", "https://example.com/test")
    return httpx.Response(
        status_code=status_code,
        headers=headers or {},
        content=content,
        request=request,
    )


# ---------------------------------------------------------------------------
# _is_json_complete (unit)
# ---------------------------------------------------------------------------


def test_is_json_complete_valid_object() -> None:
    assert _is_json_complete('{"id": "123", "name": "alice"}') is True


def test_is_json_complete_valid_array() -> None:
    assert _is_json_complete('[1, 2, 3]') is True


def test_is_json_complete_unclosed_brace() -> None:
    assert _is_json_complete('{"id": "123"') is False


def test_is_json_complete_unclosed_bracket() -> None:
    assert _is_json_complete('[1, 2,') is False


def test_is_json_complete_unclosed_string() -> None:
    assert _is_json_complete('{"id": "123}') is False


def test_is_json_complete_trailing_comma_structurally_looks_ok() -> None:
    """Trailing commas are invalid JSON but structurally the braces/strings close.
    The real json.loads check in _guard_response catches this as json_parse_error."""
    assert _is_json_complete('{"id": "123",}') is True


def test_is_json_complete_empty() -> None:
    assert _is_json_complete("") is True


# ---------------------------------------------------------------------------
# _guard_response (unit)
# ---------------------------------------------------------------------------


def test_guard_response_valid_json_passes() -> None:
    resp = _mock_response(
        b'{"id": "123", "name": "alice"}',
        headers={"content-type": "application/json"},
    )
    _guard_response(resp)  # should not raise


def test_guard_response_content_length_mismatch() -> None:
    resp = _mock_response(
        b'{"id": "123"}',
        headers={"content-length": "50", "content-type": "application/json"},
    )
    with pytest.raises(PayloadIncompleteError) as exc_info:
        _guard_response(resp)
    assert exc_info.value.violation == "content_length_mismatch"


def test_guard_response_truncated_json() -> None:
    resp = _mock_response(
        b'{"id": "123", "name": "al',
        headers={"content-type": "application/json"},
    )
    with pytest.raises(PayloadIncompleteError) as exc_info:
        _guard_response(resp)
    assert exc_info.value.violation == "json_truncated"


def test_guard_response_json_parse_error() -> None:
    resp = _mock_response(
        b'not json at all',
        headers={"content-type": "application/json"},
    )
    with pytest.raises(PayloadIncompleteError) as exc_info:
        _guard_response(resp)
    assert exc_info.value.violation == "json_parse_error"


def test_guard_response_trailing_comma_caught_by_json_loads() -> None:
    resp = _mock_response(
        b'{"id": "123",}',
        headers={"content-type": "application/json"},
    )
    with pytest.raises(PayloadIncompleteError) as exc_info:
        _guard_response(resp)
    assert exc_info.value.violation == "json_parse_error"


def test_guard_response_empty_json_body() -> None:
    resp = _mock_response(
        b"",
        headers={"content-type": "application/json"},
    )
    with pytest.raises(PayloadIncompleteError) as exc_info:
        _guard_response(resp)
    assert exc_info.value.violation == "json_empty_body"


def test_guard_response_non_json_ignores_json_checks() -> None:
    resp = _mock_response(
        b"plain text response",
        headers={"content-type": "text/plain"},
    )
    _guard_response(resp)  # should not raise


# ---------------------------------------------------------------------------
# AsyncClient
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_client_valid_json_passes() -> None:
    mock_resp = _mock_response(
        b'{"id": "123"}',
        headers={"content-type": "application/json"},
    )
    with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock, return_value=mock_resp):
        async with AsyncClient() as client:
            resp = await client.get("https://example.com/test")
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_async_client_content_length_mismatch() -> None:
    mock_resp = _mock_response(
        b'{"id": "123"}',
        headers={"content-length": "50", "content-type": "application/json"},
    )
    with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock, return_value=mock_resp):
        async with AsyncClient() as client:
            with pytest.raises(PayloadIncompleteError) as exc_info:
                await client.get("https://example.com/test")
            assert exc_info.value.violation == "content_length_mismatch"


@pytest.mark.asyncio
async def test_async_client_truncated_json() -> None:
    mock_resp = _mock_response(
        b'{"id": "123", "name": "al',
        headers={"content-type": "application/json"},
    )
    with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock, return_value=mock_resp):
        async with AsyncClient() as client:
            with pytest.raises(PayloadIncompleteError) as exc_info:
                await client.get("https://example.com/test")
            assert exc_info.value.violation == "json_truncated"


@pytest.mark.asyncio
async def test_async_client_post_valid_json() -> None:
    mock_resp = _mock_response(
        b'{"created": true}',
        headers={"content-type": "application/json"},
    )
    with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock, return_value=mock_resp):
        async with AsyncClient() as client:
            resp = await client.post("https://example.com/test", json={"foo": "bar"})
            assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Sync Client
# ---------------------------------------------------------------------------


def test_sync_client_valid_json_passes() -> None:
    mock_resp = _mock_response(
        b'{"id": "123"}',
        headers={"content-type": "application/json"},
    )
    with patch.object(httpx.Client, "request", return_value=mock_resp):
        with Client() as client:
            resp = client.get("https://example.com/test")
            assert resp.status_code == 200


def test_sync_client_content_length_mismatch() -> None:
    mock_resp = _mock_response(
        b'{"id": "123"}',
        headers={"content-length": "50", "content-type": "application/json"},
    )
    with patch.object(httpx.Client, "request", return_value=mock_resp):
        with Client() as client:
            with pytest.raises(PayloadIncompleteError) as exc_info:
                client.get("https://example.com/test")
            assert exc_info.value.violation == "content_length_mismatch"


def test_sync_client_truncated_json() -> None:
    mock_resp = _mock_response(
        b'{"results": [{"id": 1, "name": "al',
        headers={"content-type": "application/json"},
    )
    with patch.object(httpx.Client, "request", return_value=mock_resp):
        with Client() as client:
            with pytest.raises(PayloadIncompleteError) as exc_info:
                client.get("https://example.com/test")
            assert exc_info.value.violation == "json_truncated"


def test_sync_client_empty_json_body() -> None:
    mock_resp = _mock_response(
        b"",
        headers={"content-type": "application/json"},
    )
    with patch.object(httpx.Client, "request", return_value=mock_resp):
        with Client() as client:
            with pytest.raises(PayloadIncompleteError) as exc_info:
                client.get("https://example.com/test")
            assert exc_info.value.violation == "json_empty_body"


# ---------------------------------------------------------------------------
# @protect integration: PayloadIncompleteError clears cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_protect_payload_incomplete_error_clears_cache_and_logs() -> None:
    """PayloadIncompleteError inside a @protect tool is treated like any other
    exception: cache entry is removed, cache_error is logged, and the error is
    re-raised so the agent can retry."""
    calls = [0]

    @protect(entity_param="id", ttl=60)
    async def fetch(id: str) -> dict:
        calls[0] += 1
        if calls[0] == 1:
            raise PayloadIncompleteError("truncated", "json_truncated")
        return {"id": id, "ok": True}

    async with Session() as s:
        with pytest.raises(PayloadIncompleteError):
            await fetch(id="t1")
        result = await fetch(id="t1")

    assert result == {"id": "t1", "ok": True}
    assert calls[0] == 2
    assert any(e["event"] == "cache_error" for e in s.audit_log())


@pytest.mark.asyncio
async def test_protect_content_length_mismatch_via_http_client() -> None:
    """End-to-end: AsyncClient raises PayloadIncompleteError, @protect catches it
    as a cache_error, and the next call retries successfully."""
    calls = [0]
    mock_resp = _mock_response(
        b'{"id": "123"}',
        headers={"content-length": "50", "content-type": "application/json"},
    )

    @protect(entity_param="id", ttl=60)
    async def fetch(id: str) -> dict:
        calls[0] += 1
        async with AsyncClient() as client:
            return (await client.get("https://example.com/test")).json()

    with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock, return_value=mock_resp):
        async with Session() as s:
            with pytest.raises(PayloadIncompleteError):
                await fetch(id="t1")

    # After the error, a second call with a good response should succeed
    good_resp = _mock_response(
        b'{"id": "123", "name": "alice"}',
        headers={"content-type": "application/json"},
    )
    with patch.object(httpx.AsyncClient, "request", new_callable=AsyncMock, return_value=good_resp):
        async with Session() as s2:
            result = await fetch(id="t1")
            assert result == {"id": "123", "name": "alice"}
            assert calls[0] == 2
            assert any(e["event"] == "cache_add" for e in s2.audit_log())


def test_protect_sync_payload_incomplete_error_clears_cache() -> None:
    """Sync path: PayloadIncompleteError also clears cache entry."""
    from mycelium.protect import _session_var

    calls = [0]

    @protect_sync(entity_param="id", ttl=60)
    def fetch(id: str) -> dict:
        calls[0] += 1
        if calls[0] == 1:
            raise PayloadIncompleteError("truncated", "json_truncated")
        return {"id": id, "ok": True}

    session = Session()
    token = _session_var.set(session)
    try:
        with pytest.raises(PayloadIncompleteError):
            fetch(id="t1")
        result = fetch(id="t1")
        assert result == {"id": "t1", "ok": True}
        assert calls[0] == 2
        assert any(e["event"] == "cache_error" for e in session.audit_log())
    finally:
        _session_var.reset(token)
