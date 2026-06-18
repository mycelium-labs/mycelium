"""YAML configuration loader for Mycelium guards."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from mycelium.action_ledger import (
    FileLedgerStorage,
    InMemoryLedgerStorage,
    LedgerStorage,
    ledger,
    ledger_sync,
)
from mycelium.history_guard import HistoryGuard
from mycelium.message_validator import MessageValidator
from mycelium.protect import protect, protect_sync
from mycelium.task_ledger import (
    TaskFileLedgerStorage,
    TaskInMemoryLedgerStorage,
    TaskLedgerStorage,
    task_ledger,
    task_ledger_sync,
)
from mycelium.tool_boundary import bounded, bounded_sync
from mycelium.tool_registry import ToolRegistry
from mycelium.tool_runner import ToolRunner


class ConfigError(Exception):
    """Raised when a Mycelium config file is invalid or inconsistent."""


@dataclass(frozen=True)
class ToolConfig:
    """Parsed configuration for a single tool."""

    name: str
    protect: dict[str, Any] | None = None
    bounded: dict[str, Any] | None = None
    ledger: dict[str, Any] | None = None

    def is_noop(self) -> bool:
        return self.protect is None and self.bounded is None and self.ledger is None


@dataclass(frozen=True)
class TaskConfig:
    """Parsed configuration for a single task."""

    name: str
    ledger: dict[str, Any] | None = None

    def is_noop(self) -> bool:
        return self.ledger is None


@dataclass(frozen=True)
class MyceliumConfig:
    """Loaded Mycelium YAML configuration."""

    tools: dict[str, ToolConfig]
    registry_allowed: list[str]
    runner_settings: dict[str, Any]
    history_guard: dict[str, Any] | None = None
    message_validator: bool = False
    tasks: dict[str, TaskConfig] | None = None

    def apply(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """
        Decorator that applies configured guards to a function.

        Looks up the tool by ``func.__name__``. If no config exists, the
        function is returned unchanged.

        Guard order (outermost first):
        ``@ledger`` -> ``@bounded`` -> ``@protect`` -> ``func``
        """
        name = func.__name__
        tool_config = self.tools.get(name)
        if tool_config is None or tool_config.is_noop():
            return func

        is_async = inspect.iscoroutinefunction(func)

        # Apply protect first so it sits inside bounded.
        if tool_config.protect is not None:
            if is_async:
                func = protect(**tool_config.protect)(func)
            else:
                func = protect_sync(**tool_config.protect)(func)

        if tool_config.bounded is not None:
            bounded_kwargs = dict(tool_config.bounded)
            if is_async:
                func = bounded(**bounded_kwargs)(func)
            else:
                func = bounded_sync(**bounded_kwargs)(func)

        if tool_config.ledger is not None:
            storage = self._build_ledger_storage(tool_config.ledger)
            if is_async:
                func = ledger(storage=storage)(func)
            else:
                func = ledger_sync(storage=storage)(func)

        return func

    def apply_task(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator that applies configured task-level guards to a function."""
        name = func.__name__
        if self.tasks is None:
            return func
        task_config = self.tasks.get(name)
        if task_config is None or task_config.is_noop():
            return func

        is_async = inspect.iscoroutinefunction(func)
        storage = self._build_task_ledger_storage(task_config.ledger)
        id_from = list(task_config.ledger.get("id_from", [])) if task_config.ledger else []

        if is_async:
            return task_ledger(storage=storage, id_from=id_from)(func)
        return task_ledger_sync(storage=storage, id_from=id_from)(func)

    @property
    def registry(self) -> ToolRegistry:
        """Build a ToolRegistry from the configured allowlist."""
        return ToolRegistry(allowed=self.registry_allowed)

    def build_runner(self, registry: ToolRegistry | None = None) -> ToolRunner:
        """Build a ToolRunner using the configured retry settings."""
        return ToolRunner(
            registry=registry if registry is not None else self.registry,
            **self.runner_settings,
        )

    def build_history_guard(self) -> HistoryGuard | None:
        """Build a HistoryGuard if the config declares one."""
        if self.history_guard is None:
            return None
        return HistoryGuard(**self.history_guard)

    def build_message_validator(self) -> MessageValidator | None:
        """Build a MessageValidator if the config declares one."""
        if not self.message_validator:
            return None
        return MessageValidator()

    @staticmethod
    def _build_ledger_storage(raw: dict[str, Any]) -> LedgerStorage:
        """Build a LedgerStorage from tool ledger config."""
        storage_type = raw.get("storage", "memory")
        if storage_type == "file":
            path = raw.get("path")
            if not path:
                raise ConfigError("ledger storage 'file' requires a 'path'")
            return FileLedgerStorage(path)
        if storage_type == "memory":
            return InMemoryLedgerStorage()
        raise ConfigError(f"unknown ledger storage type: {storage_type!r}")

    @staticmethod
    def _build_task_ledger_storage(raw: dict[str, Any] | None) -> TaskLedgerStorage:
        """Build a TaskLedgerStorage from task ledger config."""
        if raw is None:
            return TaskInMemoryLedgerStorage()
        storage_type = raw.get("storage", "memory")
        if storage_type == "file":
            path = raw.get("path")
            if not path:
                raise ConfigError("task ledger storage 'file' requires a 'path'")
            return TaskFileLedgerStorage(path)
        if storage_type == "memory":
            return TaskInMemoryLedgerStorage()
        raise ConfigError(f"unknown task ledger storage type: {storage_type!r}")

    def wrap_module(self, module: Any) -> Any:
        """
        Apply configured guards to every callable in a module whose name
        appears in the tools map.

        Returns a simple namespace object that exposes the original module's
        attributes, with configured tools replaced by their guarded versions.
        The original module is not mutated.
        """
        namespace: dict[str, Any] = {}
        for name in dir(module):
            if name.startswith("_"):
                continue
            obj = getattr(module, name)
            if name in self.tools and callable(obj):
                namespace[name] = self.apply(obj)
            else:
                namespace[name] = obj
        return _SimpleNamespace(**namespace)


class _SimpleNamespace:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


def _parse_tool_config(name: str, raw: dict[str, Any] | None) -> ToolConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"tool '{name}' config must be a mapping")

    protect = raw.get("protect")
    bounded = raw.get("bounded")
    ledger_raw = raw.get("ledger")

    if protect is not None and not isinstance(protect, dict):
        raise ConfigError(f"tool '{name}'.protect must be a mapping")
    if bounded is not None and not isinstance(bounded, dict):
        raise ConfigError(f"tool '{name}'.bounded must be a mapping")

    ledger = _normalize_ledger_config(name, ledger_raw)

    return ToolConfig(name=name, protect=protect, bounded=bounded, ledger=ledger)


def _parse_task_config(name: str, raw: dict[str, Any] | None) -> TaskConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"task '{name}' config must be a mapping")

    ledger_raw = raw.get("ledger")
    ledger = _normalize_ledger_config(name, ledger_raw)
    return TaskConfig(name=name, ledger=ledger)


def _normalize_ledger_config(name: str, raw: Any) -> dict[str, Any] | None:
    """Convert user-friendly ledger config into a normalized dict."""
    if raw is None or raw is False:
        return None
    if raw is True:
        return {"storage": "memory"}
    if isinstance(raw, dict):
        return dict(raw)
    raise ConfigError(f"tool '{name}'.ledger must be a bool or a mapping")


def _parse_config(data: dict[str, Any]) -> MyceliumConfig:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    tools_raw = data.get("tools", {})
    if not isinstance(tools_raw, dict):
        raise ConfigError("'tools' must be a mapping")

    tools = {
        name: _parse_tool_config(name, cfg)
        for name, cfg in tools_raw.items()
    }

    tasks_raw = data.get("tasks", {})
    if not isinstance(tasks_raw, dict):
        raise ConfigError("'tasks' must be a mapping")
    tasks = {
        name: _parse_task_config(name, cfg)
        for name, cfg in tasks_raw.items()
    }

    registry_raw = data.get("registry", {})
    if not isinstance(registry_raw, dict):
        raise ConfigError("'registry' must be a mapping")
    registry_allowed = registry_raw.get("allowed", []) or []
    if not isinstance(registry_allowed, list):
        raise ConfigError("'registry.allowed' must be a list")

    runner_raw = data.get("runner", {})
    if not isinstance(runner_raw, dict):
        raise ConfigError("'runner' must be a mapping")

    history_guard_raw = data.get("history_guard")
    if history_guard_raw is not None and not isinstance(history_guard_raw, dict):
        raise ConfigError("'history_guard' must be a mapping")

    message_validator_raw = data.get("message_validator", False)
    if isinstance(message_validator_raw, dict):
        message_validator = bool(message_validator_raw.get("enabled", True))
    else:
        message_validator = bool(message_validator_raw)

    return MyceliumConfig(
        tools=tools,
        tasks=tasks,
        registry_allowed=registry_allowed,
        runner_settings=runner_raw,
        history_guard=history_guard_raw,
        message_validator=message_validator,
    )


def load_config_from_string(text: str) -> MyceliumConfig:
    """Parse Mycelium config from a YAML string."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc

    if data is None:
        data = {}

    return _parse_config(data)


def load_config(path: str | Path) -> MyceliumConfig:
    """Load Mycelium config from a YAML file."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")

    text = path.read_text(encoding="utf-8")
    return load_config_from_string(text)


__all__ = [
    "ConfigError",
    "MyceliumConfig",
    "ToolConfig",
    "load_config",
    "load_config_from_string",
]
