"""User-defined tool allowlists for AF-004."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from mycelium.tool_boundary import ToolBoundaryError


class ToolRegistry:
    """
    User-defined allowlist of tools an agent may call.

    The developer registers or lists allowed tool names; Mycelium enforces
    the list before dispatch.
    """

    def __init__(self, allowed: Iterable[str] | None = None) -> None:
        self._allowed: set[str] = set(allowed or [])

    @property
    def allowed_tools(self) -> frozenset[str]:
        return frozenset(self._allowed)

    def allow(self, tool_name: str) -> None:
        self._allowed.add(tool_name)

    def register(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Add a tool by function name and return the function unchanged."""
        self._allowed.add(func.__name__)
        return func

    def validate_call(self, tool_name: str) -> None:
        if tool_name in self._allowed:
            return

        if not self._allowed:
            allowed_text = "(none configured)"
            recovery = "Register allowed tools on ToolRegistry before dispatch."
        else:
            allowed_text = ", ".join(sorted(self._allowed))
            recovery = f"Use one of the allowed tools: {allowed_text}."

        raise ToolBoundaryError(
            f"{tool_name}: not_in_allowlist",
            violation="not_in_allowlist",
            tool_name=tool_name,
            llm_message=(
                f"Tool {tool_name!r} is not available for this agent. "
                f"Allowed tools: {allowed_text}. {recovery}"
            ),
            recovery_hint=recovery,
        )
