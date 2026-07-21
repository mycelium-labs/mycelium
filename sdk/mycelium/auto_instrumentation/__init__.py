"""Command-based Mycelium auto-instrumentation."""

from mycelium.auto_instrumentation.bootstrap import (
    AUTO_CONFIG_ENV,
    AUTO_ENABLED_ENV,
    initialize_from_environment,
    instrument_configured_callables,
)

__all__ = [
    "AUTO_CONFIG_ENV",
    "AUTO_ENABLED_ENV",
    "initialize_from_environment",
    "instrument_configured_callables",
]
