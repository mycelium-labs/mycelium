"""@bounded: tool boundary validation (input, output, scope)."""

from __future__ import annotations

import functools
import inspect
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ParamSpec, TypeVar

from pydantic import BaseModel, ValidationError

from mycelium.schema import ToolSchema, fields_to_model

P = ParamSpec("P")
R = TypeVar("R")


class ToolBoundaryError(Exception):
    """Raised when tool inputs or outputs fail boundary checks."""

    def __init__(
        self,
        message: str,
        *,
        violation: str,
        tool_name: str,
        llm_message: str,
        field: str | None = None,
        expected: str | None = None,
        actual: str | None = None,
        recovery_hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.violation = violation
        self.tool_name = tool_name
        self.llm_message = llm_message
        self.field = field
        self.expected = expected
        self.actual = actual
        self.recovery_hint = recovery_hint


class ToolBoundaryExhaustedError(Exception):
    """Raised when LLM/tool retries are exhausted."""

    def __init__(self, message: str, *, last_error: ToolBoundaryError) -> None:
        super().__init__(message)
        self.last_error = last_error


@dataclass(frozen=True)
class BoundedConfig:
    input_schema: type[BaseModel]
    output_schema: type[BaseModel] | None = None
    entity_param: str | None = None
    entity_pattern: str | None = None
    allowed_paths: tuple[str, ...] | None = None
    path_param: str = "path"


def _field_name(location: Any) -> str | None:
    if not location:
        return None
    parts = [str(part) for part in location if str(part) != "__root__"]
    return ".".join(parts) if parts else None


def _format_actual(value: Any) -> str:
    if value is None:
        return "null"
    return repr(value)


def _make_boundary_error(
    tool_name: str,
    violation: str,
    llm_parts: list[str],
    *,
    field: str | None = None,
    expected: str | None = None,
    actual: Any | None = None,
    recovery_hint: str | None = None,
) -> ToolBoundaryError:
    if recovery_hint:
        llm_parts.append(recovery_hint)
    message = f"{tool_name}: {violation}" + (f" on {field}" if field else "")
    return ToolBoundaryError(
        message,
        violation=violation,
        tool_name=tool_name,
        llm_message=" ".join(llm_parts),
        field=field,
        expected=expected,
        actual=_format_actual(actual) if actual is not None else None,
        recovery_hint=recovery_hint,
    )


def _pydantic_error_to_boundary(
    exc: ValidationError,
    tool_name: str,
    schema: type[BaseModel],
    raw: dict[str, Any],
    *,
    phase: str = "input",
) -> ToolBoundaryError:
    errors = exc.errors()
    first = errors[0]
    field = _field_name(first.get("loc"))
    error_type = str(first.get("type", "validation_error"))
    actual = first.get("input")
    if field and field in raw:
        actual = raw[field]
    expected: str | None = None
    recovery_hint: str | None = None

    if error_type == "missing":
        violation = "missing_required_field"
        expected = "a value for this field"
        recovery_hint = f"Provide {field} when calling {tool_name}."
    elif error_type == "string_type":
        violation = "type_mismatch"
        expected = "string"
        recovery_hint = f"Provide {field} as a string."
    elif error_type == "string_pattern_mismatch":
        violation = "pattern_mismatch"
        ctx = first.get("ctx") or {}
        expected = f"string matching pattern {ctx.get('pattern')!r}"
        recovery_hint = f"Provide {field} in the required format."
    else:
        violation = "output_validation_failed" if phase == "output" else error_type
        recovery_hint = (
            f"Fix the value of {field or 'the tool arguments'} and call {tool_name} again."
        )

    prefix = (
        f"Tool {tool_name!r} returned invalid output."
        if phase == "output"
        else f"Tool {tool_name!r} failed input validation."
    )
    llm_parts = [prefix]
    if field:
        llm_parts.append(f"Field: {field}.")
    if field and field in raw:
        llm_parts.append(f"Received: {_format_actual(raw[field])}.")
    elif actual is not None:
        llm_parts.append(f"Received: {_format_actual(actual)}.")
    if expected:
        llm_parts.append(f"Expected: {expected}.")

    return _make_boundary_error(
        tool_name,
        violation,
        llm_parts,
        field=field,
        expected=expected,
        actual=raw.get(field) if field and field in raw else actual,
        recovery_hint=recovery_hint,
    )


def _bind_kwargs_for_schema(
    schema: type[BaseModel],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    field_names = list(schema.model_fields)
    params = [
        inspect.Parameter(name, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        for name in field_names
    ]
    sig = inspect.Signature(params)
    bound = sig.bind_partial(*args, **kwargs)
    bound.apply_defaults()
    return {name: bound.arguments[name] for name in field_names if name in bound.arguments}


def _validate_input(tool_name: str, schema: type[BaseModel], raw: dict[str, Any]) -> None:
    try:
        schema.model_validate(raw)
    except ValidationError as exc:
        raise _pydantic_error_to_boundary(exc, tool_name, schema, raw) from exc


def _validate_scope(tool_name: str, raw: dict[str, Any], config: BoundedConfig) -> None:
    if config.entity_param and config.entity_pattern:
        value = raw.get(config.entity_param)
        if value is not None and not re.fullmatch(config.entity_pattern, str(value)):
            raise _make_boundary_error(
                tool_name,
                "scope_entity_pattern",
                [
                    f"Tool {tool_name!r} failed scope validation.",
                    f"Field: {config.entity_param}.",
                    f"Received: {_format_actual(value)}.",
                    f"Expected: string matching pattern {config.entity_pattern!r}.",
                ],
                field=config.entity_param,
                expected=f"pattern {config.entity_pattern!r}",
                actual=value,
                recovery_hint=(
                    f"Provide {config.entity_param} matching {config.entity_pattern!r}."
                ),
            )

    if config.allowed_paths and config.path_param in raw:
        path = str(raw[config.path_param])
        if not any(path.startswith(prefix) for prefix in config.allowed_paths):
            allowed = ", ".join(config.allowed_paths)
            raise _make_boundary_error(
                tool_name,
                "scope_path",
                [
                    f"Tool {tool_name!r} failed scope validation.",
                    f"Field: {config.path_param}.",
                    f"Received: {path!r}.",
                    f"Expected: path under one of [{allowed}].",
                ],
                field=config.path_param,
                expected=f"path under {allowed}",
                actual=path,
                recovery_hint=f"Retry with a path under one of: {allowed}.",
            )


def _validate_output(tool_name: str, result: Any, output_schema: type[BaseModel]) -> None:
    try:
        if isinstance(result, BaseModel):
            output_schema.model_validate(result.model_dump())
        else:
            output_schema.model_validate(result)
    except ValidationError as exc:
        raw = result if isinstance(result, dict) else {"result": result}
        raise _pydantic_error_to_boundary(
            exc, tool_name, output_schema, raw, phase="output"
        ) from exc


def _validate_before_call(
    tool_name: str,
    config: BoundedConfig,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> None:
    raw = _bind_kwargs_for_schema(config.input_schema, args, kwargs)
    _validate_input(tool_name, config.input_schema, raw)
    _validate_scope(tool_name, raw, config)


def _mark_bounded(wrapper: Callable[..., Any], config: BoundedConfig) -> None:
    wrapper._mycelium_bounded = True  # type: ignore[attr-defined]
    wrapper._mycelium_bounded_config = config  # type: ignore[attr-defined]


def get_bounded_config(func: Callable[..., Any]) -> BoundedConfig | None:
    return getattr(func, "_mycelium_bounded_config", None)


def tool_error_message(tool_call_id: str, llm_message: str) -> dict[str, str]:
    """Format a tool error for appending to agent messages before an LLM retry."""
    return {"role": "tool", "tool_call_id": tool_call_id, "content": llm_message}


def bounded(
    *,
    schema: ToolSchema,
    output_schema: ToolSchema | None = None,
    entity_param: str | None = None,
    entity_pattern: str | None = None,
    allowed_paths: list[str] | None = None,
    path_param: str = "path",
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """
    Validate tool inputs (and optionally outputs and scope) around an async tool.

    Pass field specs as plain dicts: no Pydantic types required in user code.
    Input/scope failures prevent the function from running.
    Output failures raise after the function returns; result is not propagated.
    """

    config = BoundedConfig(
        input_schema=fields_to_model(schema, model_name="BoundedInput"),
        output_schema=(
            fields_to_model(output_schema, model_name="BoundedOutput")
            if output_schema is not None
            else None
        ),
        entity_param=entity_param,
        entity_pattern=entity_pattern,
        allowed_paths=tuple(allowed_paths) if allowed_paths else None,
        path_param=path_param,
    )

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        tool_name = func.__name__

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            _validate_before_call(tool_name, config, args, kwargs)
            result = await func(*args, **kwargs)
            if config.output_schema is not None:
                _validate_output(tool_name, result, config.output_schema)
            return result

        _mark_bounded(wrapper, config)
        return wrapper

    return decorator


def bounded_sync(
    *,
    schema: ToolSchema,
    output_schema: ToolSchema | None = None,
    entity_param: str | None = None,
    entity_pattern: str | None = None,
    allowed_paths: list[str] | None = None,
    path_param: str = "path",
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Sync variant of @bounded."""

    config = BoundedConfig(
        input_schema=fields_to_model(schema, model_name="BoundedInput"),
        output_schema=(
            fields_to_model(output_schema, model_name="BoundedOutput")
            if output_schema is not None
            else None
        ),
        entity_param=entity_param,
        entity_pattern=entity_pattern,
        allowed_paths=tuple(allowed_paths) if allowed_paths else None,
        path_param=path_param,
    )

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        tool_name = func.__name__

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            _validate_before_call(tool_name, config, args, kwargs)
            result = func(*args, **kwargs)
            if config.output_schema is not None:
                _validate_output(tool_name, result, config.output_schema)
            return result

        _mark_bounded(wrapper, config)
        return wrapper

    return decorator
