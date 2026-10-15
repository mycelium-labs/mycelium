"""OpenAPI ``DecimalValue`` pattern agrees with the runtime decimal-1 check."""

from __future__ import annotations

import re

import pytest

from mycelium.sidecar import SidecarError, _validate_typed_values, openapi_document


def _pattern() -> str:
    schema = openapi_document()["components"]["schemas"]["DecimalValue"]
    return schema["properties"]["value"]["pattern"]


def _runtime_accepts(text: str) -> bool:
    try:
        _validate_typed_values({"$type": "decimal", "profile": "decimal-1", "value": text})
    except SidecarError:
        return False
    return True


@pytest.mark.parametrize(
    "text",
    ["0", "1", "-1", "10", "1.5", "-1.5", "0.1", "-0.1", "-0.001", "123.0045"],
)
def test_canonical_decimal_is_accepted_by_schema_and_runtime(text: str) -> None:
    assert re.search(_pattern(), text)
    assert _runtime_accepts(text)


@pytest.mark.parametrize(
    "text",
    ["-0", "-0.0", "00", "01", "-01", "1.", ".5", "1.50", "0.0", "+1", "1e3", "1.5e2", ""],
)
def test_non_canonical_decimal_is_rejected_by_schema_and_runtime(text: str) -> None:
    assert not re.search(_pattern(), text)
    assert not _runtime_accepts(text)
