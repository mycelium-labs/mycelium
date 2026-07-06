"""YAML configuration loader for Mycelium guards."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from contextlib import AbstractContextManager
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
from mycelium.audit_receipt import (
    AuditReceiptEmitter,
    AuditReceiptStorage,
    FileAuditReceiptStorage,
    InMemoryAuditReceiptStorage,
    resolve_signing_key,
)
from mycelium.history_guard import HistoryGuard
from mycelium.message_validator import MessageValidator
from mycelium.protect import protect, protect_sync
from mycelium.session import Session
from mycelium.state_flush import (
    FileStateFlushStorage,
    InMemoryStateFlushStorage,
    StateFlush,
    StateFlushStorage,
    get_active_flush_run,
)
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
from mycelium.transition import (
    SideEffectClass,
    SideEffectBoundary,
    RetryPermission,
    ToolTransitionBinding,
    TransitionConfig,
    TransitionScope,
    parse_retry_permission,
    parse_side_effect_boundary,
    parse_side_effect_class,
    execution_scope,
)


class ConfigError(Exception):
    """Raised when a Mycelium config file is invalid or inconsistent."""


@dataclass(frozen=True)
class ToolConfig:
    """Parsed configuration for a single tool."""

    name: str
    protect: dict[str, Any] | None = None
    bounded: dict[str, Any] | None = None
    ledger: dict[str, Any] | None = None
    audit_receipt: bool = False
    side_effect_class: SideEffectClass | None = None
    retry_permission: RetryPermission | None = None
    side_effect_boundary: SideEffectBoundary | None = None

    def is_noop(self) -> bool:
        return (
            self.protect is None
            and self.bounded is None
            and self.ledger is None
            and not self.audit_receipt
        )


@dataclass(frozen=True)
class TaskConfig:
    """Parsed configuration for a single task."""

    name: str
    ledger: dict[str, Any] | None = None
    audit_receipt: bool = False

    def is_noop(self) -> bool:
        return self.ledger is None and not self.audit_receipt


@dataclass
class MyceliumConfig:
    """Loaded Mycelium YAML configuration."""

    tools: dict[str, ToolConfig]
    registry_allowed: list[str]
    runner_settings: dict[str, Any]
    history_guard: dict[str, Any] | None = None
    message_validator: bool = False
    tasks: dict[str, TaskConfig] | None = None
    state_flush: dict[str, Any] | None = None
    audit_receipt: dict[str, Any] | None = None
    transition: TransitionConfig | None = None
    action_ledger: dict[str, Any] | None = None
    task_ledger_defaults: dict[str, Any] | None = None
    _audit_emitter: AuditReceiptEmitter | None = None
    _state_flush: StateFlush | None = None
    _audit_auto: bool = False

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
            audit_emitter = self._tool_audit_emitter(tool_config)
            transition_binding = self.tool_transition_binding(tool_config)
            ledger_kwargs = self._ledger_timing_kwargs()
            if is_async:
                func = ledger(
                    storage=storage,
                    audit_emitter=audit_emitter,
                    transition_binding=transition_binding,
                    **ledger_kwargs,
                )(func)
            else:
                func = ledger_sync(
                    storage=storage,
                    audit_emitter=audit_emitter,
                    transition_binding=transition_binding,
                    **ledger_kwargs,
                )(func)

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
        audit_emitter = self._task_audit_emitter(task_config)

        if task_config.ledger is None and task_config.audit_receipt:
            raise ConfigError(
                f"task '{name}' declares audit_receipt but has no ledger"
            )

        if task_config.ledger is None:
            return func

        if is_async:
            return task_ledger(storage=storage, id_from=id_from, audit_emitter=audit_emitter)(func)
        return task_ledger_sync(storage=storage, id_from=id_from, audit_emitter=audit_emitter)(func)

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

    def build_state_flush(self) -> StateFlush | None:
        """Build a StateFlush if the config declares one."""
        if self.state_flush is None:
            return None
        if self._state_flush is not None:
            return self._state_flush
        storage = self._build_state_flush_storage(self.state_flush)
        flush_on = self.state_flush.get("flush_on")
        if flush_on is not None and not isinstance(flush_on, list):
            raise ConfigError("'state_flush.flush_on' must be a list")
        flush_on_complete = bool(self.state_flush.get("flush_on_complete", True))
        self._state_flush = StateFlush(
            storage=storage,
            flush_on=list(flush_on) if flush_on is not None else None,
            flush_on_complete=flush_on_complete,
        )
        return self._state_flush

    def build_audit_receipt(self) -> AuditReceiptEmitter | None:
        """Build an AuditReceiptEmitter if the config declares one."""
        if self.audit_receipt is None:
            return None
        if self._audit_emitter is not None:
            return self._audit_emitter
        if self.audit_receipt.get("agent_id"):
            raise ConfigError(
                "'audit_receipt.agent_id' is no longer supported; "
                "set 'transition.agent_id' instead"
            )
        if self.transition is None:
            raise ConfigError(
                "'transition' with 'agent_id' is required when audit_receipt is configured"
            )
        agent_id = self.transition.agent_id
        signing_key = resolve_signing_key(
            signing_key=self.audit_receipt.get("signing_key"),
            signing_key_env=self.audit_receipt.get("signing_key_env"),
        )
        storage = self._build_audit_receipt_storage(self.audit_receipt)
        self._audit_emitter = AuditReceiptEmitter(
            agent_id=str(agent_id),
            signing_key=signing_key,
            storage=storage,
        )
        return self._audit_emitter

    def prepare_messages(self, messages: list[Any]) -> list[Any]:
        """
        Run configured message and history guards on a message list before the LLM call.

        When a StateFlush run is active, the validated messages are recorded
        automatically so developers do not need manual ``run.record()`` calls.
        """
        validator = self.build_message_validator()
        if validator is not None:
            messages = validator.repair(messages)

        guard = self.build_history_guard()
        if guard is not None:
            messages = guard.validate(messages)

        active_run = get_active_flush_run()
        if active_run is not None:
            active_run.record({"messages": messages})

        return messages

    def run(self, run_id: str, *, use_session: bool = True) -> AbstractContextManager[Any]:
        """
        Enter an agent run scope.

        Nests Session (cache isolation) and StateFlush when configured.
        Returns the StateFlush run handle, or a no-op handle when state_flush
        is not configured.
        """
        state_flush = self.build_state_flush()
        scope = TransitionScope(thread_id=run_id, run_id=run_id)
        if state_flush is not None:
            inner: AbstractContextManager[Any] = state_flush.run(
                run_id, use_session=use_session
            )
        elif use_session:
            inner = Session()
        else:
            inner = _NoopRun(run_id)
        return _ScopedRunContext(inner, scope)

    def tool_transition_binding(
        self, tool_config: ToolConfig
    ) -> ToolTransitionBinding | None:
        """Build per-tool transition binding when transition config is present."""
        if self.transition is None or tool_config.side_effect_class is None:
            return None
        return ToolTransitionBinding.for_tool(
            agent_id=self.transition.agent_id,
            policy_version=self.transition.policy_version,
            side_effect_class=tool_config.side_effect_class,
            scope_from=dict(self.transition.scope_from),
            retry_permission=tool_config.retry_permission,
            side_effect_boundary=tool_config.side_effect_boundary,
        )

    def _ledger_timing_kwargs(self) -> dict[str, float]:
        """Return ActionLedger timing overrides from ``transition`` config."""
        if self.transition is None:
            return {}
        kwargs: dict[str, float] = {}
        if self.transition.lease_ttl is not None:
            kwargs["lease_ttl"] = self.transition.lease_ttl
        if self.transition.poll_interval is not None:
            kwargs["poll_interval"] = self.transition.poll_interval
        if self.transition.poll_timeout is not None:
            kwargs["poll_timeout"] = self.transition.poll_timeout
        return kwargs

    def _tool_audit_emitter(self, tool_config: ToolConfig) -> AuditReceiptEmitter | None:
        if not tool_config.audit_receipt:
            return None
        if tool_config.ledger is None:
            raise ConfigError(
                f"tool '{tool_config.name}' has audit_receipt enabled but no ledger"
            )
        return self._shared_audit_emitter()

    def _task_audit_emitter(self, task_config: TaskConfig) -> AuditReceiptEmitter | None:
        if not task_config.audit_receipt:
            return None
        if task_config.ledger is None:
            raise ConfigError(
                f"task '{task_config.name}' has audit_receipt enabled but no ledger"
            )
        return self._shared_audit_emitter()

    def _shared_audit_emitter(self) -> AuditReceiptEmitter:
        emitter = self.build_audit_receipt()
        if emitter is None:
            raise ConfigError(
                "audit_receipt is enabled for a tool/task but no global "
                "'audit_receipt' section is configured"
            )
        return emitter

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
        if storage_type == "redis":
            from mycelium.storage._helpers import resolve_storage_url
            from mycelium.storage.redis_ledger import RedisLedgerStorage

            try:
                url = resolve_storage_url(raw)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            ttl = raw.get("in_flight_ttl", 3600)
            return RedisLedgerStorage(
                url,
                prefix=str(raw.get("prefix", "mycelium:action:")),
                in_flight_ttl=float(ttl) if ttl is not None else None,
            )
        if storage_type == "postgres":
            from mycelium.storage._helpers import resolve_storage_url
            from mycelium.storage.postgres_ledger import PostgresLedgerStorage

            try:
                dsn = resolve_storage_url(raw, url_key="dsn")
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            return PostgresLedgerStorage(
                dsn,
                table=str(raw.get("table", "mycelium_action_ledger")),
            )
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
        if storage_type == "redis":
            from mycelium.storage._helpers import resolve_storage_url
            from mycelium.storage.redis_ledger import RedisTaskLedgerStorage

            try:
                url = resolve_storage_url(raw)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            ttl = raw.get("in_flight_ttl", 3600)
            return RedisTaskLedgerStorage(
                url,
                prefix=str(raw.get("prefix", "mycelium:task:")),
                in_flight_ttl=float(ttl) if ttl is not None else None,
            )
        if storage_type == "postgres":
            from mycelium.storage._helpers import resolve_storage_url
            from mycelium.storage.postgres_ledger import PostgresTaskLedgerStorage

            try:
                dsn = resolve_storage_url(raw, url_key="dsn")
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            return PostgresTaskLedgerStorage(
                dsn,
                table=str(raw.get("table", "mycelium_task_ledger")),
            )
        raise ConfigError(f"unknown task ledger storage type: {storage_type!r}")

    @staticmethod
    def _build_state_flush_storage(raw: dict[str, Any]) -> StateFlushStorage:
        storage_type = raw.get("storage", "memory")
        if storage_type == "file":
            path = raw.get("path")
            if not path:
                raise ConfigError("state_flush storage 'file' requires a 'path'")
            return FileStateFlushStorage(path)
        if storage_type == "memory":
            return InMemoryStateFlushStorage()
        raise ConfigError(f"unknown state_flush storage type: {storage_type!r}")

    @staticmethod
    def _build_audit_receipt_storage(raw: dict[str, Any]) -> AuditReceiptStorage:
        storage_type = raw.get("storage", "memory")
        if storage_type == "file":
            path = raw.get("path")
            if not path:
                raise ConfigError("audit_receipt storage 'file' requires a 'path'")
            return FileAuditReceiptStorage(path)
        if storage_type == "memory":
            return InMemoryAuditReceiptStorage()
        raise ConfigError(f"unknown audit_receipt storage type: {storage_type!r}")

    def wrap_module(self, module: Any) -> Any:
        """
        Apply configured guards to every callable in a module whose name
        appears in the tools map.

        Prefer :meth:`instrument` when you also configure tasks.
        """
        return self.instrument(module, tasks=False)

    def instrument(self, module: Any, *, tasks: bool = True) -> Any:
        """
        Apply configured tool and task guards to callables in a module.

        This is the lowest-friction integration path: import your module,
        call ``config.instrument(my_tools)``, and use the returned namespace.
        """
        namespace: dict[str, Any] = {}
        task_map = self.tasks or {}
        for name in dir(module):
            if name.startswith("_"):
                continue
            obj = getattr(module, name)
            if not callable(obj):
                namespace[name] = obj
                continue
            if name in self.tools:
                namespace[name] = self.apply(obj)
            elif tasks and name in task_map:
                namespace[name] = self.apply_task(obj)
            else:
                namespace[name] = obj
        return _SimpleNamespace(**namespace)


class _SimpleNamespace:
    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)


class _ScopedRunContext(AbstractContextManager[Any]):
    """Nest an execution scope around a run/session context manager."""

    def __init__(
        self,
        inner: AbstractContextManager[Any],
        scope: TransitionScope,
    ) -> None:
        self._inner = inner
        self._scope_cm = execution_scope(scope)

    def __enter__(self) -> Any:
        self._scope_cm.__enter__()
        return self._inner.__enter__()

    def __exit__(self, *args: Any) -> bool:
        try:
            return bool(self._inner.__exit__(*args))
        finally:
            self._scope_cm.__exit__(*args)


class _NoopRun:
    """Stand-in run handle when state_flush is not configured."""

    def __init__(self, run_id: str = "") -> None:
        self.run_id = run_id

    def record(self, patch: dict[str, Any]) -> None:
        return None

    @property
    def state(self) -> dict[str, Any]:
        return {}

    def __enter__(self) -> _NoopRun:
        return self

    def __exit__(self, *_: Any) -> bool:
        return False


def _storage_settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Strip integration-only keys from a global ledger/flush section."""
    if cfg is None:
        return {"storage": "memory"}
    return {
        key: value
        for key, value in cfg.items()
        if key not in ("tools", "tasks", "auto")
    }


def _merge_storage_settings(
    base: dict[str, Any] | None,
    override: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(_storage_settings(base))
    merged.update(override)
    return merged


def _parse_tool_config(
    name: str,
    raw: dict[str, Any] | None,
    *,
    action_ledger_global: dict[str, Any] | None,
    audit_auto: bool,
) -> ToolConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"tool '{name}' config must be a mapping")

    protect = raw.get("protect")
    bounded = raw.get("bounded")
    ledger_raw = raw.get("ledger")
    audit_receipt = bool(raw.get("audit_receipt", False))

    if protect is not None and not isinstance(protect, dict):
        raise ConfigError(f"tool '{name}'.protect must be a mapping")
    if bounded is not None and not isinstance(bounded, dict):
        raise ConfigError(f"tool '{name}'.bounded must be a mapping")

    ledger = _normalize_ledger_config(name, ledger_raw, action_ledger_global)
    if audit_auto and ledger is not None and raw.get("audit_receipt") is not False:
        audit_receipt = True

    side_effect_class: SideEffectClass | None = None
    if "side_effect_class" in raw:
        try:
            side_effect_class = parse_side_effect_class(raw["side_effect_class"])
        except ValueError as exc:
            raise ConfigError(f"tool '{name}': {exc}") from exc

    retry_permission: RetryPermission | None = None
    if "retry_permission" in raw:
        try:
            retry_permission = parse_retry_permission(raw["retry_permission"])
        except ValueError as exc:
            raise ConfigError(f"tool '{name}': {exc}") from exc

    side_effect_boundary: SideEffectBoundary | None = None
    if "side_effect_boundary" in raw:
        try:
            side_effect_boundary = parse_side_effect_boundary(
                raw["side_effect_boundary"]
            )
        except ValueError as exc:
            raise ConfigError(f"tool '{name}': {exc}") from exc

    return ToolConfig(
        name=name,
        protect=protect,
        bounded=bounded,
        ledger=ledger,
        audit_receipt=audit_receipt,
        side_effect_class=side_effect_class,
        retry_permission=retry_permission,
        side_effect_boundary=side_effect_boundary,
    )


def _parse_task_config(
    name: str,
    raw: dict[str, Any] | None,
    *,
    task_ledger_global: dict[str, Any] | None,
    audit_auto: bool,
) -> TaskConfig:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"task '{name}' config must be a mapping")

    ledger_raw = raw.get("ledger")
    id_from = raw.get("id_from")
    if id_from is not None:
        if ledger_raw is None:
            ledger_raw = {"id_from": id_from}
        elif ledger_raw is True:
            ledger_raw = {"id_from": id_from}
        elif isinstance(ledger_raw, dict):
            ledger_raw = {**ledger_raw, "id_from": id_from}
    audit_receipt = bool(raw.get("audit_receipt", False))
    ledger = _normalize_ledger_config(name, ledger_raw, task_ledger_global)
    if audit_auto and ledger is not None and raw.get("audit_receipt") is not False:
        audit_receipt = True

    return TaskConfig(name=name, ledger=ledger, audit_receipt=audit_receipt)


def _normalize_ledger_config(
    name: str,
    raw: Any,
    global_cfg: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Convert user-friendly ledger config into a normalized dict."""
    if raw is None or raw is False:
        return None
    if raw is True:
        return _storage_settings(global_cfg)
    if isinstance(raw, dict):
        return _merge_storage_settings(global_cfg, raw)
    raise ConfigError(f"tool '{name}'.ledger must be a bool or a mapping")


def _apply_action_ledger_tools(
    tools: dict[str, ToolConfig],
    action_ledger: dict[str, Any],
    *,
    audit_auto: bool,
) -> None:
    apply_to = action_ledger.get("tools")
    if apply_to is None:
        return

    if apply_to == "all":
        names = list(tools.keys())
    elif isinstance(apply_to, list):
        names = [str(item) for item in apply_to]
    else:
        raise ConfigError("'action_ledger.tools' must be 'all' or a list of tool names")

    storage = _storage_settings(action_ledger)
    for name in names:
        existing = tools.get(name)
        if existing is None:
            tools[name] = ToolConfig(
                name=name,
                ledger=storage,
                audit_receipt=audit_auto,
            )
            continue
        ledger = existing.ledger if existing.ledger is not None else storage
        audit_receipt = existing.audit_receipt or (audit_auto and ledger is not None)
        tools[name] = ToolConfig(
            name=existing.name,
            protect=existing.protect,
            bounded=existing.bounded,
            ledger=ledger,
            audit_receipt=audit_receipt,
            side_effect_class=existing.side_effect_class,
            retry_permission=existing.retry_permission,
            side_effect_boundary=existing.side_effect_boundary,
        )


def _apply_task_ledger_tasks(
    tasks: dict[str, TaskConfig],
    task_ledger: dict[str, Any],
    *,
    audit_auto: bool,
) -> None:
    apply_to = task_ledger.get("tasks")
    if apply_to is None:
        return

    if apply_to == "all":
        names = list(tasks.keys())
    elif isinstance(apply_to, list):
        names = [str(item) for item in apply_to]
    else:
        raise ConfigError("'task_ledger.tasks' must be 'all' or a list of task names")

    storage = _storage_settings(task_ledger)
    for name in names:
        existing = tasks.get(name)
        if existing is None:
            tasks[name] = TaskConfig(
                name=name,
                ledger=storage,
                audit_receipt=audit_auto,
            )
            continue
        ledger = existing.ledger if existing.ledger is not None else storage
        audit_receipt = existing.audit_receipt or (audit_auto and ledger is not None)
        tasks[name] = TaskConfig(
            name=existing.name,
            ledger=ledger,
            audit_receipt=audit_receipt,
        )


def _parse_optional_positive_float(
    raw: dict[str, Any],
    key: str,
    *,
    section: str,
    allow_null: bool = False,
) -> float | None:
    if key not in raw:
        return None
    value = raw[key]
    if value is None:
        if allow_null:
            return None
        raise ConfigError(f"'{section}.{key}' cannot be null")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{section}.{key}' must be a number") from exc
    if parsed <= 0:
        raise ConfigError(f"'{section}.{key}' must be greater than zero")
    return parsed


def _parse_transition_config(raw: Any) -> TransitionConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'transition' must be a mapping")

    agent_id = raw.get("agent_id")
    policy_version = raw.get("policy_version")
    if not agent_id:
        raise ConfigError("'transition.agent_id' is required")
    if not policy_version:
        raise ConfigError("'transition.policy_version' is required")

    scope_from_raw = raw.get("scope_from", {})
    if not isinstance(scope_from_raw, dict):
        raise ConfigError("'transition.scope_from' must be a mapping")
    scope_from = {str(key): str(value) for key, value in scope_from_raw.items()}

    lease_ttl = _parse_optional_positive_float(
        raw, "lease_ttl", section="transition"
    )
    poll_interval = _parse_optional_positive_float(
        raw, "poll_interval", section="transition"
    )
    poll_timeout = _parse_optional_positive_float(
        raw, "poll_timeout", section="transition"
    )

    return TransitionConfig(
        agent_id=str(agent_id),
        policy_version=str(policy_version),
        scope_from=scope_from,
        lease_ttl=lease_ttl,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
    )


def _validate_transition_tools(
    tools: dict[str, ToolConfig],
    transition: TransitionConfig | None,
) -> None:
    if transition is None:
        return
    for name, tool in tools.items():
        if tool.ledger is not None and tool.side_effect_class is None:
            raise ConfigError(
                f"tool '{name}' has ledger but no side_effect_class; "
                "required when 'transition' is configured"
            )


def _parse_config(data: dict[str, Any]) -> MyceliumConfig:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    action_ledger_raw = data.get("action_ledger")
    if action_ledger_raw is not None and not isinstance(action_ledger_raw, dict):
        raise ConfigError("'action_ledger' must be a mapping")

    task_ledger_raw = data.get("task_ledger")
    if task_ledger_raw is not None and not isinstance(task_ledger_raw, dict):
        raise ConfigError("'task_ledger' must be a mapping")

    transition_raw = data.get("transition")
    transition = _parse_transition_config(transition_raw)

    audit_receipt_raw = data.get("audit_receipt")
    if audit_receipt_raw is not None and not isinstance(audit_receipt_raw, dict):
        raise ConfigError("'audit_receipt' must be a mapping")
    if audit_receipt_raw and audit_receipt_raw.get("agent_id"):
        raise ConfigError(
            "'audit_receipt.agent_id' is no longer supported; "
            "set 'transition.agent_id' instead"
        )

    audit_auto = bool(audit_receipt_raw and audit_receipt_raw.get("auto", True))

    tools_raw = data.get("tools", {})
    if not isinstance(tools_raw, dict):
        raise ConfigError("'tools' must be a mapping")

    tools = {
        name: _parse_tool_config(
            name,
            cfg,
            action_ledger_global=action_ledger_raw,
            audit_auto=audit_auto,
        )
        for name, cfg in tools_raw.items()
    }

    if action_ledger_raw:
        _apply_action_ledger_tools(tools, action_ledger_raw, audit_auto=audit_auto)

    _validate_transition_tools(tools, transition)

    tasks_raw = data.get("tasks", {})
    if not isinstance(tasks_raw, dict):
        raise ConfigError("'tasks' must be a mapping")
    tasks = {
        name: _parse_task_config(
            name,
            cfg,
            task_ledger_global=task_ledger_raw,
            audit_auto=audit_auto,
        )
        for name, cfg in tasks_raw.items()
    }

    if task_ledger_raw:
        _apply_task_ledger_tasks(tasks, task_ledger_raw, audit_auto=audit_auto)

    registry_raw = data.get("registry", {})
    if not isinstance(registry_raw, dict):
        raise ConfigError("'registry' must be a mapping")
    registry_allowed = registry_raw.get("allowed", []) or []
    if not isinstance(registry_allowed, list):
        raise ConfigError("'registry.allowed' must be a list")
    if registry_raw.get("auto") and not registry_allowed:
        registry_allowed = list(tools.keys())

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

    state_flush_raw = data.get("state_flush")
    if state_flush_raw is not None and not isinstance(state_flush_raw, dict):
        raise ConfigError("'state_flush' must be a mapping")

    return MyceliumConfig(
        tools=tools,
        tasks=tasks,
        registry_allowed=registry_allowed,
        runner_settings=runner_raw,
        history_guard=history_guard_raw,
        message_validator=message_validator,
        state_flush=state_flush_raw,
        audit_receipt=audit_receipt_raw,
        transition=transition,
        action_ledger=action_ledger_raw,
        task_ledger_defaults=task_ledger_raw,
        _audit_auto=audit_auto,
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
    "TransitionConfig",
    "load_config",
    "load_config_from_string",
]
