"""Trust labels for values crossing agent and tool boundaries."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

UNTRUSTED_DATA = "untrusted_data"


@dataclass(frozen=True)
class UntrustedToolOutput:
    """Tool-returned content that may be read but never treated as authority."""

    value: Any
    source: str
    label: str = UNTRUSTED_DATA

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("source must not be empty")
        if self.label != UNTRUSTED_DATA:
            raise ValueError("tool output must use the untrusted_data label")

    def as_authorization_evidence(self) -> None:
        """Fail closed if a caller tries to promote tool content to authority."""
        raise TypeError("untrusted tool output cannot be authorization evidence")


def mark_untrusted_tool_output(value: Any, *, source: str) -> UntrustedToolOutput:
    """Attach the untrusted-data label to a value returned by a tool."""
    return UntrustedToolOutput(value=value, source=source)


def contains_untrusted_tool_output(value: Any) -> bool:
    """Return whether a value contains a marked tool output."""
    if isinstance(value, UntrustedToolOutput):
        return True
    if isinstance(value, Mapping):
        return any(
            contains_untrusted_tool_output(key)
            or contains_untrusted_tool_output(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(contains_untrusted_tool_output(item) for item in value)
    return False


__all__ = [
    "UNTRUSTED_DATA",
    "UntrustedToolOutput",
    "contains_untrusted_tool_output",
    "mark_untrusted_tool_output",
]
