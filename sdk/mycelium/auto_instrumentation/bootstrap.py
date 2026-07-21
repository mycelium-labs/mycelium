"""Bootstrap configured callables before the target application starts."""

from __future__ import annotations

import importlib
import os
from types import ModuleType
from typing import Any

from mycelium.config import (
    AutoInstrumentationTarget,
    ConfigError,
    MyceliumConfig,
    load_config,
)

AUTO_ENABLED_ENV = "MYCELIUM_AUTO_INSTRUMENT"
AUTO_CONFIG_ENV = "MYCELIUM_AUTO_CONFIG"


def _resolve_module_attribute(
    target: AutoInstrumentationTarget,
) -> tuple[ModuleType, Any]:
    module_name, attribute = target.callable_path.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ConfigError(
            f"cannot import {target.kind} {target.name!r} module "
            f"{module_name!r}: {exc}"
        ) from exc
    if not hasattr(module, attribute):
        raise ConfigError(
            f"{target.kind} {target.name!r} callable "
            f"{target.callable_path!r} does not exist"
        )
    value = getattr(module, attribute)
    if not callable(value) or not hasattr(value, "__name__"):
        raise ConfigError(
            f"{target.kind} {target.name!r} target "
            f"{target.callable_path!r} is not a function"
        )
    return module, value


def instrument_configured_callables(config: MyceliumConfig) -> None:
    """Eagerly import, validate, and replace every configured tool/task."""
    for target in config.auto_instrumentation_targets():
        module, func = _resolve_module_attribute(target)
        if target.kind == "tool":
            wrapped = config.apply_tool(target.name, func)
        else:
            wrapped = config.apply_named_task(target.name, func)
        _, attribute = target.callable_path.split(":", 1)
        setattr(module, attribute, wrapped)


def initialize_from_environment() -> None:
    """Initialize once when loaded by the launcher's ``sitecustomize``."""
    enabled = os.environ.pop(AUTO_ENABLED_ENV, None)
    config_path = os.environ.pop(AUTO_CONFIG_ENV, None)
    if enabled != "1":
        return
    if not config_path:
        raise ConfigError(f"{AUTO_CONFIG_ENV} is required for auto-instrumentation")
    config = load_config(config_path)
    instrument_configured_callables(config)
