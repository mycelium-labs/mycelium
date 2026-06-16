"""ToolRunner — AF-004 retry orchestration with optional LLM recovery."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from mycelium.tool_boundary import (
    ToolBoundaryError,
    ToolBoundaryExhaustedError,
    tool_error_message,
)
from mycelium.tool_registry import ToolRegistry

_OUTPUT_VIOLATIONS = frozenset({"output_validation_failed"})


class ToolRunner:
    """
    Execute tools with registry checks, output retries, and optional LLM recovery.

    Input, allowlist, and scope failures are LLM-recoverable via run_with_llm_retry().
    Output failures retry the tool up to max_tool_retries, then follow the LLM path.
    """

    def __init__(
        self,
        registry: ToolRegistry | None = None,
        *,
        max_llm_retries: int = 2,
        max_tool_retries: int = 3,
    ) -> None:
        self._registry = registry
        self._max_llm_retries = max_llm_retries
        self._max_tool_retries = max_tool_retries

    def _validate_allowlist(self, tool_name: str) -> None:
        if self._registry is not None:
            self._registry.validate_call(tool_name)

    async def call(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Call a tool with allowlist check and output-validation retries."""
        self._validate_allowlist(func.__name__)

        last_error: ToolBoundaryError | None = None
        for attempt in range(self._max_tool_retries):
            try:
                return await self._invoke(func, *args, **kwargs)
            except ToolBoundaryError as exc:
                last_error = exc
                if exc.violation not in _OUTPUT_VIOLATIONS:
                    raise
                if attempt >= self._max_tool_retries - 1:
                    raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("unreachable")

    async def run_with_llm_retry(
        self,
        func: Callable[..., Any],
        /,
        *,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        kwargs: dict[str, Any],
        invoke_llm: Callable[[list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]],
        parse_tool_kwargs: Callable[[list[dict[str, Any]], str], dict[str, Any]],
    ) -> tuple[Any, list[dict[str, Any]]]:
        """
        Run a tool with LLM recovery on boundary failures.

        On ToolBoundaryError, appends a tool error message and calls invoke_llm
        so the model can correct args or pick an allowed tool.
        """
        current_messages = list(messages)
        current_kwargs = dict(kwargs)
        last_error: ToolBoundaryError | None = None

        for llm_attempt in range(self._max_llm_retries + 1):
            try:
                result = await self.call(func, **current_kwargs)
                return result, current_messages
            except ToolBoundaryError as exc:
                last_error = exc
                current_messages.append(tool_error_message(tool_call_id, exc.llm_message))
                if llm_attempt >= self._max_llm_retries:
                    break
                current_messages = list(await invoke_llm(current_messages))
                current_kwargs = parse_tool_kwargs(current_messages, func.__name__)

        assert last_error is not None
        raise ToolBoundaryExhaustedError(
            f"Tool {func.__name__!r} failed after {self._max_llm_retries} LLM retries.",
            last_error=last_error,
        ) from last_error

    def call_sync(self, func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        """Sync variant of call()."""
        self._validate_allowlist(func.__name__)

        last_error: ToolBoundaryError | None = None
        for attempt in range(self._max_tool_retries):
            try:
                return func(*args, **kwargs)
            except ToolBoundaryError as exc:
                last_error = exc
                if exc.violation not in _OUTPUT_VIOLATIONS:
                    raise
                if attempt >= self._max_tool_retries - 1:
                    raise
        if last_error is not None:
            raise last_error
        raise RuntimeError("unreachable")

    @staticmethod
    async def _invoke(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
        result = func(*args, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
