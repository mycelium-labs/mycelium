"""Protection modules (AF-* aligned)."""

from .context_corruption import (
    AccessDecision,
    ContextCache,
    ContextSegmentation,
    Criticality,
    InvalidationPolicy,
    InvalidationReason,
)
from .decorators import ToolMetadata, ToolRegistry, protect, tool

__all__ = [
    "ContextCache",
    "ContextSegmentation",
    "Criticality",
    "InvalidationPolicy",
    "InvalidationReason",
    "AccessDecision",
    "tool",
    "protect",
    "ToolRegistry",
    "ToolMetadata",
]
