"""
HTTP transport wrapper — automatic payload completeness detection.

Drop-in replacements for ``httpx.AsyncClient`` and ``httpx.Client`` that check
for the most common silent corruption patterns at the transport boundary:

  * **Content-Length mismatch** — the server promised N bytes but fewer arrived.
  * **JSON structural truncation** — unclosed braces, strings, or arrays in a
    response that claims to be JSON.
  * **Empty body** — a JSON response with zero bytes when content was expected.

When a problem is detected a ``PayloadIncompleteError`` is raised. If the call
is wrapped with ``@protect`` the error is treated like any other exception:
the cache entry is cleared, ``cache_error`` is logged, and the agent sees a
regular exception it can retry.

Usage::

    from mycelium.http import AsyncClient, PayloadIncompleteError

    @protect(entity_param="customer_id", ttl=60)
    async def fetch_customer(customer_id: str) -> dict:
        async with AsyncClient() as client:
            resp = await client.get(f"https://api.example.com/customers/{customer_id}")
            return resp.json()

Sync frameworks (CrewAI, Smolagents) use ``Client`` instead of ``AsyncClient``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class PayloadIncompleteError(Exception):
    """Tool returned a payload that failed transport-level completeness checks."""

    def __init__(self, reason: str, violation: str) -> None:
        super().__init__(reason)
        self.violation = violation


# ---------------------------------------------------------------------------
# JSON structural completeness (no parser dependency)
# ---------------------------------------------------------------------------


def _is_json_complete(text: str) -> bool:
    """Heuristic: detect unclosed strings, braces, brackets, or trailing commas.

    Returns ``True`` when the text *appears* structurally complete.
    Returns ``False`` when common truncation signatures are present.
    """
    text = text.strip()
    if not text:
        return True

    # Unclosed string
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
    if in_string:
        return False

    # Brace / bracket balance
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in "([{":
            stack.append(ch)
        elif ch in ")]}":
            if not stack:
                return False
            opener = stack.pop()
            if (
                (ch == ")" and opener != "(")
                or (ch == "]" and opener != "[")
                or (ch == "}" and opener != "{")
            ):
                return False
    if stack:
        return False

    # Trailing comma before end
    last_non_ws = ""
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string and not ch.isspace():
            last_non_ws = ch
    if last_non_ws == ",":
        return False

    return True


def _check_json_complete(raw_text: str) -> None:
    """Raise PayloadIncompleteError if raw_text looks like truncated JSON."""
    if not _is_json_complete(raw_text):
        raise PayloadIncompleteError(
            "JSON response appears structurally truncated "
            "(unclosed braces, strings, arrays, or trailing comma).",
            violation="json_truncated",
        )


# ---------------------------------------------------------------------------
# Shared guard logic
# ---------------------------------------------------------------------------


def _guard_response(response: httpx.Response) -> None:
    """Run all automatic completeness checks on a fully-read response."""
    body = response.content

    # 1. Content-Length mismatch
    content_length = response.headers.get("content-length")
    if content_length is not None:
        expected = int(content_length)
        actual = len(body)
        if actual < expected:
            raise PayloadIncompleteError(
                f"Content-Length mismatch: expected {expected} bytes, "
                f"got {actual} bytes. Response was truncated in transit.",
                violation="content_length_mismatch",
            )

    # 2. JSON completeness
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        if len(body) == 0:
            raise PayloadIncompleteError(
                "JSON response has empty body (0 bytes).",
                violation="json_empty_body",
            )
        raw = body.decode("utf-8", errors="replace")
        _check_json_complete(raw)
        # Also verify it actually parses — structural check can have false negatives
        try:
            json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PayloadIncompleteError(
                f"JSON response failed to parse: {exc}",
                violation="json_parse_error",
            ) from exc


# ---------------------------------------------------------------------------
# Async client
# ---------------------------------------------------------------------------


class AsyncClient:
    """Drop-in replacement for ``httpx.AsyncClient`` with automatic payload
    completeness checks.

    Every request is validated after the body arrives:

    * ``Content-Length`` mismatch → ``PayloadIncompleteError``
    * JSON truncation or parse failure → ``PayloadIncompleteError``
    * Empty JSON body → ``PayloadIncompleteError``

    Usage::

        async with AsyncClient() as client:
            resp = await client.get("https://api.example.com/data")
            data = resp.json()
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._client = httpx.AsyncClient(*args, **kwargs)

    async def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        kwargs.pop("stream", None)  # not supported by httpx 0.28+ request()
        response = await self._client.request(method, url, *args, **kwargs)
        _guard_response(response)
        return response

    async def get(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", url, *args, **kwargs)

    async def post(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", url, *args, **kwargs)

    async def put(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("PUT", url, *args, **kwargs)

    async def patch(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", url, *args, **kwargs)

    async def delete(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", url, *args, **kwargs)

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Sync client
# ---------------------------------------------------------------------------


class Client:
    """Drop-in replacement for ``httpx.Client`` with automatic payload
    completeness checks.

    Same validation rules as ``AsyncClient`` but for synchronous code.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._client = httpx.Client(*args, **kwargs)

    def request(self, method: str, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        kwargs.pop("stream", None)  # not supported by httpx 0.28+ request()
        response = self._client.request(method, url, *args, **kwargs)
        _guard_response(response)
        return response

    def get(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, *args, **kwargs)

    def post(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, *args, **kwargs)

    def put(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, *args, **kwargs)

    def patch(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", url, *args, **kwargs)

    def delete(self, url: str, *args: Any, **kwargs: Any) -> httpx.Response:
        return self.request("DELETE", url, *args, **kwargs)

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: Any) -> None:
        self._client.close()
