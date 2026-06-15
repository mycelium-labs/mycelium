"""Mycelium SDK — runtime failure prevention for AI agents."""

from mycelium.history_guard import HistoryGuard, HistoryTruncatedError
from mycelium.message_validator import MessageValidationError, MessageValidator
from mycelium.protect import protect, protect_sync
from mycelium.session import Session
from mycelium.tool_boundary import (
    ToolBoundaryError,
    ToolBoundaryExhaustedError,
    bounded,
    bounded_sync,
    tool_error_message,
)
from mycelium.tool_registry import ToolRegistry
from mycelium.tool_runner import ToolRunner

__version__ = "0.1.0"

__all__ = [
    "protect",
    "protect_sync",
    "Session",
    "MessageValidator",
    "MessageValidationError",
    "HistoryGuard",
    "HistoryTruncatedError",
    "bounded",
    "bounded_sync",
    "ToolBoundaryError",
    "ToolBoundaryExhaustedError",
    "tool_error_message",
    "ToolRegistry",
    "ToolRunner",
    "__version__",
]
