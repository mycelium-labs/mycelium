"""Internal schema helpers: converts user field specs to Pydantic models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, create_model

FieldSpec = dict[str, Any]
ToolSchema = dict[str, FieldSpec]

_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def fields_to_model(fields: ToolSchema, *, model_name: str) -> type[BaseModel]:
    """Build a Pydantic model from a user-facing field spec dict."""
    if not fields:
        raise ValueError("schema must define at least one field")

    model_fields: dict[str, Any] = {}
    for name, spec in fields.items():
        py_type = _TYPE_MAP.get(str(spec.get("type", "string")), str)
        constraints: dict[str, Any] = {}
        if pattern := spec.get("pattern"):
            constraints["pattern"] = pattern
        if (min_length := spec.get("min_length")) is not None:
            constraints["min_length"] = min_length
        if (max_length := spec.get("max_length")) is not None:
            constraints["max_length"] = max_length

        if spec.get("required", True):
            model_fields[name] = (py_type, Field(**constraints))
        else:
            model_fields[name] = (py_type | None, Field(default=None, **constraints))

    return create_model(model_name, **model_fields)  # type: ignore[call-overload]
