"""Protection modules (AF-* aligned)."""

from .context_corruption import (
    ContextCache,
    ContextSegmentation,
    Criticality,
    InvalidationPolicy,
    InvalidationReason,
    AccessDecision,
)
from .decorators import tool, protect, ToolRegistry, ToolMetadata

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
