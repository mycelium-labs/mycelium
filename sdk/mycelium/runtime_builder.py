"""Runtime construction and security-sensitive wrapper composition."""

from __future__ import annotations

import functools
import importlib
import inspect
import math
import os
import warnings
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from mycelium.action_ledger import (
    ARGS_DRIFT_POLICIES,
    ARGS_DRIFT_SOFT,
    UNCLASSIFIED_POLICY_STRICT,
    UNCLASSIFIED_POLICY_WARN,
    FileLedgerStorage,
    InMemoryLedgerStorage,
    LedgerStorage,
    ledger,
    ledger_sync,
)
from mycelium.audit_receipt import (
    AtomicAuditReceiptStorage,
    AuditReceiptEmitter,
    AuditReceiptStorage,
    FileAuditReceiptStorage,
    InMemoryAuditReceiptStorage,
    resolve_signing_key,
)
from mycelium.authority_window import (
    USE_TIME_CHECK_REQUIRED,
    AuthorityWindowPolicy,
    set_authority_window_policy,
)
from mycelium.budget_guard import (
    ON_MISSING_HARD,
    ON_MISSING_METER_MODES,
    BudgetGuard,
    BudgetGuardStorage,
    FileBudgetGuardStorage,
    InMemoryBudgetGuardStorage,
    PostgresBudgetGuardStorage,
    RedisBudgetGuardStorage,
    SqliteBudgetGuardStorage,
    apply_budget_guard,
)
from mycelium.completion_contract import (
    AtomicCompletionStorage,
    CompletionContract,
    CompletionStorage,
    FileCompletionStorage,
    InMemoryCompletionStorage,
    registered_terminal_adapters,
    set_active_completion_contract,
)
from mycelium.config_parser import (
    _AUTHORITY_WINDOW_KEYS,  # noqa: F401  compatibility re-export
    _DEPLOYMENT_TOPOLOGIES,  # noqa: F401  compatibility re-export
    _DESTRUCTIVE_GRANT_KEYS,  # noqa: F401  compatibility re-export
    _DESTRUCTIVE_OBJECT_KEYS,  # noqa: F401  compatibility re-export
    _DESTRUCTIVE_TOOL_KEYS,  # noqa: F401  compatibility re-export
    _DESTRUCTIVE_TOP_KEYS,  # noqa: F401  compatibility re-export
    _USE_TIME_FACT_KEYS,  # noqa: F401  compatibility re-export
    _USE_TIME_SUBJECT_KEYS,  # noqa: F401  compatibility re-export
    _USE_TIME_TOP_KEYS,  # noqa: F401  compatibility re-export
    _apply_action_ledger_tools,  # noqa: F401  compatibility re-export
    _apply_task_ledger_tasks,  # noqa: F401  compatibility re-export
    _enforce_memory_storage_policy,  # noqa: F401  compatibility re-export
    _enforce_production_outcome_emit,  # noqa: F401  compatibility re-export
    _memory_storage_policy,  # noqa: F401  compatibility re-export
    _missing_run_id_policy,
    _normalize_ledger_config,  # noqa: F401  compatibility re-export
    _outcome_on_failure,
    _parse_authority_window,  # noqa: F401  compatibility re-export
    _parse_completion_id_lists,
    _parse_deployment,  # noqa: F401  compatibility re-export
    _parse_destination_spec,  # noqa: F401  compatibility re-export
    _parse_destructive_confirm,  # noqa: F401  compatibility re-export
    _parse_destructive_grant,  # noqa: F401  compatibility re-export
    _parse_destructive_object,  # noqa: F401  compatibility re-export
    _parse_entity_guard,  # noqa: F401  compatibility re-export
    _parse_integrations,  # noqa: F401  compatibility re-export
    _parse_optional_non_negative_float,  # noqa: F401  compatibility re-export
    _parse_optional_positive_float,  # noqa: F401  compatibility re-export
    _parse_profile,  # noqa: F401  compatibility re-export
    _parse_secret_args,  # noqa: F401  compatibility re-export
    _parse_string_list,  # noqa: F401  compatibility re-export
    _parse_task_config,  # noqa: F401  compatibility re-export
    _parse_tool_config,  # noqa: F401  compatibility re-export
    _parse_transition_config,  # noqa: F401  compatibility re-export
    _parse_use_time_currency,  # noqa: F401  compatibility re-export
    _parse_use_time_fact,  # noqa: F401  compatibility re-export
    _parse_verify,  # noqa: F401  compatibility re-export
    _request_identity_policy,
    _scope_grant_from_config,
    _side_effecting_memory_tools,  # noqa: F401  compatibility re-export
    _validate_callable_targets,  # noqa: F401  compatibility re-export
    _validate_transition_tools,  # noqa: F401  compatibility re-export
    authority_window_policy_from_mapping,
    destructive_confirm_policy_from_mapping,
    entity_guard_policy_from_mapping,
    secret_args_policy_from_mapping,
    use_time_currency_policy_from_mapping,
)
from mycelium.config_policies import (
    _budget_ceilings_from_config,
    _missing_usage_policy,
    _parse_callable_path,
)
from mycelium.config_schema import (
    CONFIG_VERSION,
)
from mycelium.config_types import (
    PROFILE_DEVELOPMENT,
    PROFILE_PRODUCTION,
    AutoInstrumentationTarget,
    ConfigError,
    TaskConfig,
    ToolConfig,
)
from mycelium.contracts import apply_tool_contract
from mycelium.decision import DecisionPolicyBundle, apply_decision_policy
from mycelium.destructive_confirm import (
    MISSING_POLICY_ERROR as DESTRUCTIVE_MISSING_POLICY_ERROR,
)
from mycelium.destructive_confirm import (
    STORAGE_FILE,
    STORAGE_MEMORY,
    STORAGE_POSTGRES,
    STORAGE_REDIS,
    STORAGE_SQLITE,
    FileDestructiveGrantStore,
    InMemoryDestructiveGrantStore,
    PostgresDestructiveGrantStore,
    RedisDestructiveGrantStore,
    SqliteDestructiveGrantStore,
    apply_destructive_confirm,
    destructive_confirm_policy_for_tool,
)
from mycelium.entity_guard import (
    MISSING_POLICY_ERROR,
    apply_entity_guard,
    entity_guard_policy_for_tool,
)
from mycelium.history_guard import HistoryGuard
from mycelium.integrations.crewai import (
    CrewAIIntegrationError,
    _set_active_crewai_integration,
    install_crewai_runtime,
    instrument_crewai_tool,
)
from mycelium.integrations.langgraph import (
    LangGraphIntegrationError,
    instrument_langgraph_tool,
)
from mycelium.ledger_payload import LedgerPayloadPolicy
from mycelium.loop_guard import (
    DEFAULT_CONSECUTIVE_SOFT,
    AtomicLoopGuardStorage,
    FileLoopGuardStorage,
    InMemoryLoopGuardStorage,
    LoopGuard,
    LoopGuardStorage,
    apply_loop_guard,
)
from mycelium.loop_guard import (
    UNCLASSIFIED_POLICY_STRICT as LOOP_UNCLASSIFIED_STRICT,
)
from mycelium.loop_guard import (
    UNCLASSIFIED_POLICY_WARN as LOOP_UNCLASSIFIED_WARN,
)
from mycelium.message_validator import MessageValidator
from mycelium.outcome_emit import (
    FileOutcomeStorage,
    InMemoryOutcomeStorage,
    OutcomeEmitter,
    OutcomeStorage,
)
from mycelium.outcome_export import (
    FanoutOutcomeStorage,
    OpenTelemetryOutcomeStorage,
    PrometheusOutcomeStorage,
    WebhookOutcomeStorage,
)
from mycelium.protect import protect, protect_sync
from mycelium.scope_guard import (
    ON_VIOLATION_MODES,
    ON_VIOLATION_SOFT,
    AtomicScopeGuardStorage,
    FileScopeGuardStorage,
    InMemoryScopeGuardStorage,
    ScopeGuard,
    ScopeGuardStorage,
    apply_scope_guard,
)
from mycelium.secret_protection import (
    apply_secret_args,
)
from mycelium.session import Session
from mycelium.state_authority import (
    ON_MISMATCH_HARD,
    ON_MISMATCH_MODES,
    StateAuthority,
    apply_state_authority,
)
from mycelium.state_flush import (
    AtomicStateFlushStorage,
    FileStateFlushStorage,
    InMemoryStateFlushStorage,
    StateFlush,
    StateFlushStorage,
    get_active_flush_run,
)
from mycelium.storage._helpers import resolve_storage_url
from mycelium.storage.atomic_state import (
    AtomicStateBackend,
    FileAtomicStateBackend,
    InMemoryAtomicStateBackend,
    PostgresAtomicStateBackend,
    RedisAtomicStateBackend,
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
    CONSEQUENTIAL_SIDE_EFFECT_CLASSES,
    ToolTransitionBinding,
    TransitionConfig,
    TransitionScope,
    execution_scope,
)
from mycelium.use_time_currency import (
    MISSING_POLICY_ERROR as USE_TIME_MISSING_POLICY_ERROR,
)
from mycelium.use_time_currency import (
    apply_use_time_currency,
    set_use_time_currency_policy,
    use_time_currency_policy_for_tool,
)

_CONFIG_APPLIED_MARKER = "_mycelium_config_applied"
_GUARD_MARKERS = (
    "_mycelium_ledger",
    "_mycelium_task_ledger",
    "_mycelium_bounded",
    "_mycelium_protected",
    "_mycelium_loop_guarded",
    "_mycelium_budget_guarded",
    "_mycelium_scope_guarded",
    "_mycelium_state_authority",
    "_mycelium_secret_args",
    "_mycelium_entity_guard",
    "_mycelium_langgraph_integration",
)


def _import_callable(callable_path: str, *, kind: str) -> Callable[..., Any]:
    module_name, attribute = callable_path.split(":", 1)
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(f"{kind} {callable_path!r} could not be imported: {exc}") from exc
    try:
        target = getattr(module, attribute)
    except AttributeError as exc:
        raise ConfigError(f"{kind} {callable_path!r} does not exist") from exc
    if not callable(target):
        raise ConfigError(f"{kind} {callable_path!r} is not callable")
    return target


def _check_existing_config_wrapper(
    func: Callable[..., Any],
    *,
    kind: str,
    name: str,
) -> bool:
    applied = getattr(func, _CONFIG_APPLIED_MARKER, None)
    if applied is not None:
        if applied == (kind, name):
            return True
        raise ConfigError(f"{kind} {name!r} is already configured as {applied[0]} {applied[1]!r}")
    if any(getattr(func, marker, False) for marker in _GUARD_MARKERS):
        raise ConfigError(
            f"{kind} {name!r} is already partially Mycelium-wrapped; "
            "use either @config.apply / @config.apply_task or 'mycelium run', "
            "not standalone guard decorators plus auto-instrumentation"
        )
    return False


def _mark_config_applied(
    func: Callable[..., Any],
    *,
    kind: str,
    name: str,
) -> None:
    setattr(func, _CONFIG_APPLIED_MARKER, (kind, name))


def _callable_with_name(
    func: Callable[..., Any],
    name: str,
) -> Callable[..., Any]:
    """Return a metadata-preserving alias whose guard identity is ``name``."""
    if func.__name__ == name:
        return func
    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_alias(*args: Any, **kwargs: Any) -> Any:
            return await func(*args, **kwargs)

        alias: Callable[..., Any] = async_alias
    else:

        @functools.wraps(func)
        def sync_alias(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        alias = sync_alias
    alias.__name__ = name
    alias.__qualname__ = name
    return alias


@dataclass
class MyceliumConfig:
    """Loaded Mycelium YAML configuration."""

    tools: dict[str, ToolConfig]
    registry_allowed: list[str]
    runner_settings: dict[str, Any]
    config_version: int = CONFIG_VERSION
    history_guard: dict[str, Any] | None = None
    message_validator: bool = False
    tasks: dict[str, TaskConfig] | None = None
    state_flush: dict[str, Any] | None = None
    audit_receipt: dict[str, Any] | None = None
    state_backend: dict[str, Any] | None = None
    outcome_emit: dict[str, Any] | None = None
    transition: TransitionConfig | None = None
    action_ledger: dict[str, Any] | None = None
    task_ledger_defaults: dict[str, Any] | None = None
    integrations: dict[str, dict[str, Any]] | None = None
    loop_guard: dict[str, Any] | None = None
    budget: dict[str, Any] | None = None
    scope_guard: dict[str, Any] | None = None
    state_authority: dict[str, Any] | None = None
    completion: dict[str, Any] | None = None
    deployment: dict[str, Any] | None = None
    verify: dict[str, Any] | None = None
    secret_args: dict[str, Any] | None = None
    entity_guard: dict[str, Any] | None = None
    destructive_confirm: dict[str, Any] | None = None
    authority_window: dict[str, Any] | None = None
    use_time_currency: dict[str, Any] | None = None
    profile: str = PROFILE_DEVELOPMENT
    _audit_emitter: AuditReceiptEmitter | None = None
    _outcome_emitter: OutcomeEmitter | None = None
    _loop_guard: LoopGuard | None = None
    _budget_guard: BudgetGuard | None = None
    _scope_guard: ScopeGuard | None = None
    _state_authority: StateAuthority | None = None
    _completion: CompletionContract | None = None
    _state_flush: StateFlush | None = None
    _state_backend: AtomicStateBackend | None = None
    _destructive_store: Any | None = None
    _audit_auto: bool = False
    _terminal_adapters: frozenset[str] = frozenset()
    _llm_adapters: frozenset[str] = frozenset()
    _crewai_runtime_installed: bool = False

    def apply(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """
        Decorator that applies configured guards to a function.

        Looks up the tool by ``func.__name__``. If no config exists, the
        function is returned unchanged.

        Guard order (outermost first, after optional LangGraph instrument):
        ``@secret_args`` -> ``@entity_guard`` -> ``@destructive_confirm`` ->
        ``@use_time_currency`` -> ``@state_authority`` -> ``@scope_guard`` ->
        ``@budget_guard`` -> ``@loop_guard`` -> ``@ledger`` -> ``@bounded`` ->
        ``@protect`` -> ``func``
        """
        return self.apply_tool(func.__name__, func)

    def loop_guard_applies(self, name: str, tool_config: ToolConfig | None = None) -> bool:
        """Whether AF-003 loop_guard should wrap this tool."""
        if self.loop_guard is None:
            return False
        cfg = tool_config if tool_config is not None else self.tools.get(name)
        if cfg is not None and cfg.loop_guard is False:
            return False
        exclude = self.loop_guard.get("exclude") or []
        if name in exclude:
            return False
        tools_sel = self.loop_guard.get("tools", "all")
        if tools_sel != "all":
            if not isinstance(tools_sel, list) or name not in tools_sel:
                return False
        return True

    def scope_guard_applies(self, name: str, tool_config: ToolConfig | None = None) -> bool:
        """Whether AF-008 scope_guard should wrap this tool."""
        if self.scope_guard is None:
            return False
        cfg = tool_config if tool_config is not None else self.tools.get(name)
        if cfg is not None and cfg.scope_guard is False:
            return False
        exclude = self.scope_guard.get("exclude") or []
        if name in exclude:
            return False
        tools_sel = self.scope_guard.get("tools", "all")
        if tools_sel != "all":
            if not isinstance(tools_sel, list) or name not in tools_sel:
                return False
        return True

    def budget_guard_applies(self, name: str, tool_config: ToolConfig | None = None) -> bool:
        """Whether budget_guard should wrap this tool."""
        if self.budget is None:
            return False
        cfg = tool_config if tool_config is not None else self.tools.get(name)
        if cfg is not None and cfg.budget_guard is False:
            return False
        exclude = self.budget.get("exclude") or []
        if name in exclude:
            return False
        tools_sel = self.budget.get("tools", "all")
        if tools_sel != "all":
            if not isinstance(tools_sel, list) or name not in tools_sel:
                return False
        return True

    def state_authority_applies(self, name: str, tool_config: ToolConfig | None = None) -> bool:
        """Whether the state-authority execution gate should wrap this tool."""
        if self.state_authority is None:
            return False
        cfg = tool_config if tool_config is not None else self.tools.get(name)
        if cfg is not None and cfg.state_authority is False:
            return False
        exclude = self.state_authority.get("exclude") or []
        if name in exclude:
            return False
        tools_sel = self.state_authority.get("tools", "all")
        if tools_sel != "all":
            if not isinstance(tools_sel, list) or name not in tools_sel:
                return False
        return True

    def secret_args_applies(self, name: str, tool_config: ToolConfig | None = None) -> bool:
        """Whether secret-in-args scanning should wrap this tool."""
        if self.secret_args is None:
            return False
        policy = secret_args_policy_from_mapping(self.secret_args)
        if not policy.enabled:
            return False
        cfg = tool_config if tool_config is not None else self.tools.get(name)
        if cfg is not None and cfg.secret_args is False:
            return False
        if name in policy.allow_tools:
            return False
        return True

    def entity_guard_applies(self, name: str, tool_config: ToolConfig | None = None) -> bool:
        """Whether destination-policy checking should wrap this tool."""
        if self.entity_guard is None:
            return False
        policy = entity_guard_policy_from_mapping(self.entity_guard)
        if not policy.enabled:
            return False
        cfg = tool_config if tool_config is not None else self.tools.get(name)
        if cfg is not None and cfg.entity_guard is False:
            return False
        return name in policy.tools

    def destructive_confirm_applies(self, name: str, tool_config: ToolConfig | None = None) -> bool:
        """Whether destructive-confirm should wrap this tool."""
        if self.destructive_confirm is None:
            return False
        policy = destructive_confirm_policy_from_mapping(self.destructive_confirm)
        if not policy.enabled:
            return False
        cfg = tool_config if tool_config is not None else self.tools.get(name)
        if cfg is not None and cfg.destructive_confirm is False:
            return False
        return name in policy.tools

    def use_time_currency_applies(self, name: str, tool_config: ToolConfig | None = None) -> bool:
        """Whether use-time currency should wrap this tool."""
        if self.use_time_currency is None:
            return False
        policy = use_time_currency_policy_from_mapping(self.use_time_currency)
        if not policy.enabled:
            return False
        cfg = tool_config if tool_config is not None else self.tools.get(name)
        if cfg is not None and cfg.use_time_currency is False:
            return False
        return name in policy.tools

    def build_destructive_grant_store(self) -> Any:
        """Build (once) the grant store declared by ``destructive_confirm:``."""
        if self._destructive_store is not None:
            return self._destructive_store
        raw = self.destructive_confirm or {}
        self._destructive_store = self._build_destructive_grant_store(raw)
        return self._destructive_store

    @staticmethod
    def _build_destructive_grant_store(raw: dict[str, Any]) -> Any:
        storage_type = raw.get("storage", STORAGE_MEMORY)
        if storage_type == STORAGE_MEMORY:
            return InMemoryDestructiveGrantStore()
        if storage_type == STORAGE_FILE:
            path = raw.get("path")
            if not path:
                raise ConfigError("destructive_confirm storage 'file' requires a 'path'")
            return FileDestructiveGrantStore(path)
        if storage_type == STORAGE_SQLITE:
            path = raw.get("path")
            if not path:
                raise ConfigError("destructive_confirm storage 'sqlite' requires a 'path'")
            return SqliteDestructiveGrantStore(
                path,
                table=str(raw.get("table", "mycelium_destructive_grants")),
            )
        if storage_type == STORAGE_REDIS:
            from mycelium.storage._helpers import resolve_storage_url

            try:
                url = resolve_storage_url(raw)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            return RedisDestructiveGrantStore(
                url,
                prefix=str(raw.get("prefix", "mycelium:destructive:")),
            )
        if storage_type == STORAGE_POSTGRES:
            from mycelium.storage._helpers import resolve_storage_url

            try:
                dsn = resolve_storage_url(raw, url_key="dsn")
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            return PostgresDestructiveGrantStore(
                dsn,
                table=str(raw.get("table", "mycelium_destructive_grants")),
            )
        raise ConfigError(f"unknown destructive_confirm storage type: {storage_type!r}")

    def _activate_authority_window(self) -> None:
        """Bind process policy for use-time expiry checks."""
        raw = self.authority_window
        if raw is None and self.destructive_confirm is not None:
            # AF-011 already promises expiry; enable use-time even when
            # authority_window: is omitted.
            set_authority_window_policy(
                AuthorityWindowPolicy(
                    enabled=True,
                    use_time_check=USE_TIME_CHECK_REQUIRED,
                    clock_skew_tolerance_seconds=0.0,
                )
            )
            return
        if raw is None:
            return
        set_authority_window_policy(authority_window_policy_from_mapping(raw))

    def _activate_use_time_currency(self) -> None:
        """Bind process policy for use-time currency checks."""
        raw = self.use_time_currency
        if raw is None:
            return
        set_use_time_currency_policy(use_time_currency_policy_from_mapping(raw))

    def apply_tool(
        self,
        name: str,
        func: Callable[..., Any],
    ) -> Callable[..., Any]:
        """Apply the tool config selected by explicit logical ``name``."""
        tool_config = self.tools.get(name)
        if tool_config is None:
            return func
        applies_loop = self.loop_guard_applies(name, tool_config)
        applies_budget = self.budget_guard_applies(name, tool_config)
        applies_scope = self.scope_guard_applies(name, tool_config)
        applies_state = self.state_authority_applies(name, tool_config)
        applies_secret = self.secret_args_applies(name, tool_config)
        applies_entity = self.entity_guard_applies(name, tool_config)
        applies_destructive = self.destructive_confirm_applies(name, tool_config)
        applies_use_time = self.use_time_currency_applies(name, tool_config)
        if tool_config.secret_fields:
            setattr(func, "_mycelium_secret_fields", tool_config.secret_fields)
        if (
            tool_config.is_noop()
            and not applies_loop
            and not applies_budget
            and not applies_scope
            and not applies_state
            and not applies_secret
            and not applies_entity
            and not applies_destructive
            and not applies_use_time
        ):
            return func
        if _check_existing_config_wrapper(func, kind="tool", name=name):
            return func

        func = _callable_with_name(func, name)
        # Keep the contract inside the ledger wrapper: claims/decision flow remains
        # authoritative, while validation still precedes the tool body.
        if tool_config.contract is not None:
            func = apply_tool_contract(func, tool_config.contract, tool_name=name)
        if tool_config.secret_fields:
            setattr(func, "_mycelium_secret_fields", tool_config.secret_fields)
        is_async = inspect.iscoroutinefunction(func)
        atomic_policy_kwargs: dict[str, Any] = {}
        uses_atomic_decision_policy = tool_config.ledger is not None
        consequential = (
            tool_config.side_effect_class in CONSEQUENTIAL_SIDE_EFFECT_CLASSES
            if tool_config.side_effect_class is not None
            else False
        )

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
            outcome_emitter = self.build_outcome_emitter()
            transition_binding = self.tool_transition_binding(tool_config)
            ledger_kwargs = self._ledger_timing_kwargs()
            action_ledger_cfg = self.action_ledger or {}
            if "unclassified_policy" in action_ledger_cfg:
                unclassified_policy = action_ledger_cfg["unclassified_policy"]
            elif self.profile == PROFILE_PRODUCTION:
                unclassified_policy = UNCLASSIFIED_POLICY_STRICT
            else:
                unclassified_policy = UNCLASSIFIED_POLICY_WARN
            ledger_kwargs["unclassified_policy"] = unclassified_policy
            on_args_drift = action_ledger_cfg.get("on_args_drift", ARGS_DRIFT_SOFT)
            if on_args_drift not in ARGS_DRIFT_POLICIES:
                raise ConfigError(
                    "'action_ledger.on_args_drift' must be one of "
                    f"{sorted(ARGS_DRIFT_POLICIES)}, got {on_args_drift!r}"
                )
            ledger_kwargs["on_args_drift"] = on_args_drift
            payload_config = action_ledger_cfg.get("payload_policy", {})
            if not isinstance(payload_config, dict) or set(payload_config) - {
                "store_args",
                "store_result",
                "redact_fields",
            }:
                raise ConfigError("'action_ledger.payload_policy' has invalid fields")
            try:
                ledger_kwargs["payload_policy"] = LedgerPayloadPolicy(**payload_config)
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"invalid action_ledger.payload_policy: {exc}") from exc
            ledger_kwargs["request_identity_policy"] = _request_identity_policy(
                action_ledger_cfg, profile=self.profile
            )
            if is_async:
                func = ledger(
                    storage=storage,
                    audit_emitter=audit_emitter,
                    outcome_emitter=outcome_emitter,
                    transition_binding=transition_binding,
                    **ledger_kwargs,
                )(func)
            else:
                func = ledger_sync(
                    storage=storage,
                    audit_emitter=audit_emitter,
                    outcome_emitter=outcome_emitter,
                    transition_binding=transition_binding,
                    **ledger_kwargs,
                )(func)

        # Loop guard outside ledger so soft/hard never claim.
        if applies_loop:
            guard = self.build_loop_guard()
            assert guard is not None
            consecutive_override: int | None = None
            if isinstance(tool_config.loop_guard, dict):
                raw_n = tool_config.loop_guard.get("consecutive_soft")
                if raw_n is not None:
                    consecutive_override = int(raw_n)
            func = apply_loop_guard(
                func,
                guard,
                tool_name=name,
                side_effect_class=tool_config.side_effect_class,
                consecutive_soft=consecutive_override,
            )

        # Budget guard outside loop/ledger: refuse next step, never mid-flight.
        if applies_budget:
            bguard = self.build_budget_guard()
            assert bguard is not None
            func = apply_budget_guard(func, bguard, tool_name=name)

        # Scope guard outside loop/ledger: frozen allowlist never claims.
        if applies_scope:
            sguard = self.build_scope_guard()
            assert sguard is not None
            func = apply_scope_guard(func, sguard, tool_name=name)

        # State authority outside loop/ledger: superseded decisions never claim.
        if applies_state:
            authority = self.build_state_authority()
            assert authority is not None
            func = apply_state_authority(
                func,
                authority,
                tool_name=name,
                side_effect_class=tool_config.side_effect_class,
            )

        # Use-time currency outside claim: authorize before claim, use inside ledger.
        if applies_use_time:
            self._activate_use_time_currency()
            policy = use_time_currency_policy_from_mapping(self.use_time_currency or {})
            if (
                self.profile == PROFILE_PRODUCTION
                and policy.missing_policy != USE_TIME_MISSING_POLICY_ERROR
            ):
                raise ConfigError(
                    f"profile is {PROFILE_PRODUCTION!r} but "
                    f"use_time_currency.missing_policy is {policy.missing_policy!r}; "
                    "production requires 'error'"
                )
            tool_policy = use_time_currency_policy_for_tool(policy, name)
            if uses_atomic_decision_policy:
                atomic_policy_kwargs["use_time_policy"] = tool_policy
            else:
                func = apply_use_time_currency(
                    func,
                    tool_policy,
                    tool_name=name,
                    outcome_emitter=self.build_outcome_emitter(),
                )

        # Destructive confirm outside claim: ungranted objects never execute.
        if applies_destructive:
            self._activate_authority_window()
            policy = destructive_confirm_policy_from_mapping(self.destructive_confirm or {})
            if (
                self.profile == PROFILE_PRODUCTION
                and policy.missing_policy != DESTRUCTIVE_MISSING_POLICY_ERROR
            ):
                raise ConfigError(
                    f"profile is {PROFILE_PRODUCTION!r} but "
                    f"destructive_confirm.missing_policy is {policy.missing_policy!r}; "
                    "production requires 'error'"
                )
            store = self.build_destructive_grant_store()
            tool_policy = destructive_confirm_policy_for_tool(policy, name)
            if uses_atomic_decision_policy:
                atomic_policy_kwargs["destructive_policy"] = tool_policy
                atomic_policy_kwargs["destructive_store"] = store
            else:
                func = apply_destructive_confirm(
                    func,
                    tool_policy,
                    tool_name=name,
                    store=store,
                    outcome_emitter=self.build_outcome_emitter(),
                )

        # Destination policy outside claim: unauthorized recipients never execute.
        if applies_entity:
            from dataclasses import replace as _replace_policy

            policy = entity_guard_policy_from_mapping(self.entity_guard or {})
            if policy.policy_version == "unspecified" and self.transition is not None:
                policy = _replace_policy(policy, policy_version=self.transition.policy_version)
            if self.profile == PROFILE_PRODUCTION and policy.missing_policy != MISSING_POLICY_ERROR:
                raise ConfigError(
                    f"profile is {PROFILE_PRODUCTION!r} but "
                    f"entity_guard.missing_policy is {policy.missing_policy!r}; "
                    "production requires 'error'"
                )
            tool_policy = entity_guard_policy_for_tool(policy, name)
            if uses_atomic_decision_policy:
                atomic_policy_kwargs["entity_policy"] = tool_policy
            else:
                func = apply_entity_guard(
                    func,
                    tool_policy,
                    tool_name=name,
                )

        # Secret-in-args outside every other guard: scan before claim/fingerprint.
        if applies_secret:
            policy = secret_args_policy_from_mapping(self.secret_args or {})
            if self.profile == PROFILE_PRODUCTION and consequential and policy.policy != "error":
                raise ConfigError(
                    f"profile is {PROFILE_PRODUCTION!r} but secret_args.policy "
                    f"is {policy.policy!r}; consequential tool {name!r} requires "
                    "'error'"
                )
            if uses_atomic_decision_policy:
                atomic_policy_kwargs["secret_policy"] = policy
                atomic_policy_kwargs["secret_fields"] = tool_config.secret_fields
                atomic_policy_kwargs["consequential"] = consequential
            else:
                func = apply_secret_args(
                    func,
                    policy,
                    tool_name=name,
                    secret_fields=tool_config.secret_fields,
                    consequential=consequential,
                )

        if atomic_policy_kwargs:
            if applies_destructive or applies_use_time:
                atomic_policy_kwargs["outcome_emitter"] = self.build_outcome_emitter()
            func = apply_decision_policy(
                func,
                DecisionPolicyBundle(**atomic_policy_kwargs),
                tool_name=name,
            )

        requires_framework_context = (
            tool_config.ledger is not None
            or applies_loop
            or applies_scope
            or applies_state
            or applies_secret
            or applies_entity
            or applies_destructive
            or applies_use_time
        )

        # LangGraph outermost so it can inject scope/dispatch before inner guards.
        if self.langgraph_enabled and requires_framework_context:
            try:
                func = instrument_langgraph_tool(func)
            except LangGraphIntegrationError as exc:
                raise ConfigError(str(exc)) from exc

        # CrewAI runtime hooks are process-scoped and preserve this callable's
        # public signature. Installing here also catches an enabled integration
        # whose optional dependency is absent before the tool can be registered.
        if self.crewai_enabled and requires_framework_context:
            try:
                func = instrument_crewai_tool(
                    func,
                    run_id_from=self.crewai_run_id_from,
                )
            except CrewAIIntegrationError as exc:
                raise ConfigError(str(exc)) from exc

        _mark_config_applied(func, kind="tool", name=name)
        return func

    @property
    def langgraph_enabled(self) -> bool:
        """Whether automatic LangGraph ToolRuntime identity is enabled."""
        if self.integrations is None:
            return False
        return bool(self.integrations.get("langgraph", {}).get("enabled", False))

    @property
    def crewai_enabled(self) -> bool:
        """Whether automatic CrewAI runtime identity is enabled."""
        if self.integrations is None:
            return False
        return bool(self.integrations.get("crewai", {}).get("enabled", False))

    @property
    def crewai_run_id_from(self) -> str | None:
        """Kickoff input field configured as CrewAI's stable run scope."""
        if not self.crewai_enabled or self.integrations is None:
            return None
        value = self.integrations.get("crewai", {}).get("run_id_from")
        return str(value) if value is not None else None

    def _activate_framework_integrations(self) -> None:
        """Activate process-scoped adapters requested by this config."""
        _set_active_crewai_integration(
            enabled=self.crewai_enabled,
            run_id_from=self.crewai_run_id_from,
        )
        self._crewai_runtime_installed = False
        if not self.crewai_enabled:
            return
        try:
            self._crewai_runtime_installed = install_crewai_runtime(
                run_id_from=self.crewai_run_id_from
            )
        except CrewAIIntegrationError as exc:
            raise ConfigError(str(exc)) from exc

    def auto_instrumentation_targets(self) -> list[AutoInstrumentationTarget]:
        """Return callable targets, requiring paths for every configured entry."""
        targets: list[AutoInstrumentationTarget] = []
        missing: list[str] = []
        for name, tool in self.tools.items():
            if tool.is_noop():
                continue
            if tool.callable_path is None:
                missing.append(f"tool {name!r}")
            else:
                targets.append(AutoInstrumentationTarget("tool", name, tool.callable_path))
        for name, task in (self.tasks or {}).items():
            if task.is_noop():
                continue
            if task.callable_path is None:
                missing.append(f"task {name!r}")
            else:
                targets.append(AutoInstrumentationTarget("task", name, task.callable_path))
        if missing:
            joined = ", ".join(missing)
            raise ConfigError(f"'mycelium run' requires callable paths for: {joined}")
        if not targets:
            raise ConfigError("'mycelium run' found no configured tool/task callable paths")
        return targets

    def apply_task(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """Decorator that applies configured task-level guards to a function."""
        return self.apply_named_task(func.__name__, func)

    def apply_named_task(
        self,
        name: str,
        func: Callable[..., Any],
    ) -> Callable[..., Any]:
        """Apply the task config selected by explicit logical ``name``."""
        if self.tasks is None:
            return func
        task_config = self.tasks.get(name)
        if task_config is None or task_config.is_noop():
            return func
        if _check_existing_config_wrapper(func, kind="task", name=name):
            return func

        func = _callable_with_name(func, name)
        is_async = inspect.iscoroutinefunction(func)
        storage = self._build_task_ledger_storage(task_config.ledger)
        id_from = list(task_config.ledger.get("id_from", [])) if task_config.ledger else []
        audit_emitter = self._task_audit_emitter(task_config)

        if task_config.ledger is None and task_config.audit_receipt:
            raise ConfigError(f"task '{name}' declares audit_receipt but has no ledger")

        if task_config.ledger is None:
            return func

        if is_async:
            func = task_ledger(
                storage=storage,
                id_from=id_from,
                audit_emitter=audit_emitter,
            )(func)
        else:
            func = task_ledger_sync(
                storage=storage,
                id_from=id_from,
                audit_emitter=audit_emitter,
            )(func)
        _mark_config_applied(func, kind="task", name=name)
        return func

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

    @staticmethod
    def _build_atomic_state_backend(raw: dict[str, Any]) -> AtomicStateBackend:
        storage_type = str(raw.get("storage", "memory"))
        if storage_type == "memory":
            return InMemoryAtomicStateBackend()
        if storage_type == "file":
            path = raw.get("path")
            if not path:
                raise ConfigError("state backend storage 'file' requires a 'path'")
            return FileAtomicStateBackend(path)
        if storage_type == "redis":
            try:
                url = resolve_storage_url(raw)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            return RedisAtomicStateBackend(
                url,
                prefix=str(raw.get("prefix", "mycelium:state:")),
            )
        if storage_type == "postgres":
            try:
                dsn = resolve_storage_url(raw, url_key="dsn", alt_keys=("url",))
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            return PostgresAtomicStateBackend(
                dsn,
                table=str(raw.get("table", "mycelium_state")),
            )
        raise ConfigError(f"unknown state backend storage type: {storage_type!r}")

    def build_state_backend(self) -> AtomicStateBackend | None:
        """Build the global state backend shared by configured guardrails."""

        if self.state_backend is None:
            return None
        if self._state_backend is None:
            self._state_backend = self._build_atomic_state_backend(self.state_backend)
        return self._state_backend

    def _guard_atomic_backend(
        self,
        raw: dict[str, Any],
    ) -> tuple[AtomicStateBackend, str] | None:
        storage_type = raw.get("storage")
        if storage_type in ("redis", "postgres"):
            backend = self._build_atomic_state_backend(raw)
            base = str(raw.get("namespace", "mycelium"))
            return backend, base
        if storage_type == "shared" or (storage_type is None and self.state_backend is not None):
            backend = self.build_state_backend()
            if backend is None:
                raise ConfigError("storage: shared requires a top-level state_backend")
            base = str((self.state_backend or {}).get("namespace", "mycelium"))
            return backend, base
        return None

    def build_loop_guard(self) -> LoopGuard | None:
        """Build a shared LoopGuard if the config declares ``loop_guard:``."""
        if self.loop_guard is None:
            return None
        if self._loop_guard is not None:
            return self._loop_guard
        raw = self.loop_guard
        shared = self._guard_atomic_backend(raw)
        storage = (
            AtomicLoopGuardStorage(shared[0], namespace=f"{shared[1]}:loop_guard")
            if shared is not None
            else self._build_loop_guard_storage(raw)
        )
        consecutive = dict(DEFAULT_CONSECUTIVE_SOFT)
        consecutive_raw = raw.get("consecutive_soft")
        if consecutive_raw is not None:
            if not isinstance(consecutive_raw, dict):
                raise ConfigError("'loop_guard.consecutive_soft' must be a mapping")
            for key, value in consecutive_raw.items():
                if not isinstance(value, int) or value < 1:
                    raise ConfigError(f"'loop_guard.consecutive_soft.{key}' must be a positive int")
                consecutive[str(key)] = int(value)
        escalate = raw.get("escalate_after_soft", 1)
        if not isinstance(escalate, int) or escalate < 1:
            raise ConfigError("'loop_guard.escalate_after_soft' must be a positive int")
        unclassified = raw.get("unclassified_policy", LOOP_UNCLASSIFIED_WARN)
        if unclassified not in (LOOP_UNCLASSIFIED_WARN, LOOP_UNCLASSIFIED_STRICT):
            raise ConfigError(
                "'loop_guard.unclassified_policy' must be "
                f"{LOOP_UNCLASSIFIED_WARN!r} or {LOOP_UNCLASSIFIED_STRICT!r}"
            )
        exclude = raw.get("exclude") or []
        if not isinstance(exclude, list):
            raise ConfigError("'loop_guard.exclude' must be a list of tool names")
        missing_run_id_policy = _missing_run_id_policy(
            raw,
            "loop_guard.missing_run_id_policy",
            profile=self.profile,
        )
        agent_id = "loop-guard"
        if self.transition is not None and self.transition.agent_id:
            agent_id = self.transition.agent_id
        self._loop_guard = LoopGuard(
            storage,
            consecutive_soft=consecutive,
            escalate_after_soft=escalate,
            unclassified_policy=str(unclassified),
            exclude=[str(item) for item in exclude],
            outcome_emitter=self.build_outcome_emitter(),
            agent_id=agent_id,
            missing_run_id_policy=missing_run_id_policy,
        )
        return self._loop_guard

    @staticmethod
    def _build_loop_guard_storage(raw: dict[str, Any]) -> LoopGuardStorage:
        storage_type = raw.get("storage", "memory")
        if storage_type == "memory":
            return InMemoryLoopGuardStorage()
        if storage_type == "file":
            path = raw.get("path")
            if not path:
                raise ConfigError("loop_guard storage 'file' requires a 'path'")
            return FileLoopGuardStorage(path)
        raise ConfigError(f"unknown loop_guard storage type: {storage_type!r}")

    def build_budget_guard(self) -> BudgetGuard | None:
        """Build a shared BudgetGuard if the config declares ``budget:``."""
        if self.budget is None:
            return None
        if self._budget_guard is not None:
            return self._budget_guard
        raw = self.budget
        storage = self._build_budget_guard_storage(raw)
        ceilings = _budget_ceilings_from_config(raw)
        warn_at = raw.get("warn_at", 0.8)
        if isinstance(warn_at, bool):
            raise ConfigError("'budget.warn_at' must be a float in (0, 1]")
        try:
            warn_at_f = float(warn_at)
        except (TypeError, ValueError) as exc:
            raise ConfigError("'budget.warn_at' must be a float in (0, 1]") from exc
        if not math.isfinite(warn_at_f) or not 0.0 < warn_at_f <= 1.0:
            raise ConfigError("'budget.warn_at' must be a float in (0, 1]")
        on_missing = raw.get("on_missing_meter", ON_MISSING_HARD)
        if on_missing not in ON_MISSING_METER_MODES:
            raise ConfigError(
                f"'budget.on_missing_meter' must be one of {sorted(ON_MISSING_METER_MODES)}"
            )
        exclude = raw.get("exclude") or []
        if not isinstance(exclude, list):
            raise ConfigError("'budget.exclude' must be a list of tool names")
        missing_usage = _missing_usage_policy(raw, profile=self.profile)
        self._budget_guard = BudgetGuard(
            storage,
            ceilings=ceilings,
            warn_at=warn_at_f,
            on_missing_meter=str(on_missing),
            missing_usage_policy=missing_usage,
            exclude=[str(item) for item in exclude],
        )
        return self._budget_guard

    @property
    def llm_budget_wired(self) -> bool:
        """Whether a real LLM adapter was verified for ``budget:``."""
        return bool(self._llm_adapters)

    def instrument_llm(
        self,
        target: Any,
        *,
        framework: str | None = None,
        scope_key: str | None = None,
        record_usage: bool = True,
    ) -> Any:
        """Wrap a model / LLM callable with ``budget.check("llm")`` auto-wiring.

        Requires a ``budget:`` block. Framework glue is LangGraph chat model,
        CrewAI LLM, or a plain callable — not per provider. See
        ``mycelium.budget_llm.instrument_llm``.
        """
        guard = self.build_budget_guard()
        if guard is None:
            raise ConfigError("instrument_llm requires a 'budget:' block in the config")
        from mycelium.budget_llm import (
            LlmBudgetAdapter,
            register_llm_budget_adapter,
        )
        from mycelium.budget_llm import (
            instrument_llm as _instrument_llm,
        )

        register_llm_budget_adapter(
            LlmBudgetAdapter(name="manual", measures_tokens=True, measures_cost=False)
        )
        return _instrument_llm(
            target,
            guard,
            framework=framework,
            scope_key=scope_key,
            record_usage=record_usage,
        )

    def _activate_llm_budget(self) -> None:
        """Bind the budget guard and verify an LLM adapter when needed."""
        import importlib

        budget_llm_mod = importlib.import_module("mycelium.budget_llm")
        install_langgraph_llm_budget = budget_llm_mod.install_langgraph_llm_budget
        registered_llm_budget_adapters = budget_llm_mod.registered_llm_budget_adapters
        set_active_budget_guard = budget_llm_mod.set_active_budget_guard

        if self.budget is None:
            set_active_budget_guard(None)
            self._llm_adapters = frozenset()
            return

        guard = self.build_budget_guard()
        set_active_budget_guard(guard)
        adapters = set(registered_llm_budget_adapters())
        install_error: str | None = None
        if self.langgraph_enabled:
            try:
                installed = install_langgraph_llm_budget()
            except Exception as exc:  # pragma: no cover - defensive
                installed = False
                install_error = str(exc)
            else:
                install_error = None
            if installed:
                adapters.add("langgraph")
        self._llm_adapters = frozenset(adapters)
        self._verify_llm_budget_coverage(adapters, install_error, budget_llm_mod)

    def _verify_llm_budget_coverage(
        self,
        adapters: set[str],
        install_error: str | None,
        budget_llm_mod: Any,
    ) -> None:
        ceilings = _budget_ceilings_from_config(self.budget or {})
        token_or_cost = ceilings.requires_usage_meter()
        if not token_or_cost:
            return

        measures_tokens = "langgraph" in adapters
        measures_cost = bool(budget_llm_mod._cost_resolvers)
        for name in adapters:
            adapter = budget_llm_mod._registered_llm_adapters.get(name)
            if adapter is None:
                continue
            measures_tokens = measures_tokens or adapter.measures_tokens
            if adapter.resolve_cost is not None:
                measures_cost = True

        if self.profile == PROFILE_PRODUCTION:
            if not adapters:
                if install_error:
                    detail = (
                        f"the LangGraph/LangChain LLM adapter was not installed ({install_error})"
                    )
                elif not self.langgraph_enabled:
                    detail = (
                        "no LLM adapter was explicitly selected. Set "
                        "integrations.langgraph.enabled: true (and install "
                        "'mycelium-runtime[langgraph]') or "
                        "register_llm_budget_adapter(...) before load_config(). "
                        "Having LangGraph installed is not enough"
                    )
                else:
                    detail = "the LangGraph/LangChain LLM adapter was not installed"
                raise ConfigError(
                    f"profile is {PROFILE_PRODUCTION!r} and 'budget:' sets "
                    f"token/cost limits, but {detail}. LLM calls would bypass "
                    "the budget."
                )
            if ceilings.max_tokens is not None and not measures_tokens:
                raise ConfigError(
                    "profile is 'production' and budget.max_tokens is set, but "
                    "the selected LLM adapter cannot measure tokens. Register "
                    "an adapter with measures_tokens=True."
                )
            if ceilings.max_usd is not None and not measures_cost:
                raise ConfigError(
                    "profile is 'production' and budget.max_usd/max_cost_usd "
                    "is set, but no cost resolver is registered. Mycelium "
                    "never invents prices — call register_llm_cost_resolver "
                    "or register_llm_budget_adapter(..., resolve_cost=...) "
                    "before load_config(). measures_cost=True without "
                    "resolve_cost is rejected. Step/time-only budgets do not "
                    "need this."
                )
            return

        if not adapters and not budget_llm_mod._unwired_llm_warned:
            warnings.warn(
                "'budget:' token/cost limits are enabled but no LLM adapter "
                "is wired; model calls are not automatically protected. "
                "Install mycelium-runtime[langgraph] or use instrument_llm / "
                "register_llm_budget_adapter. Development mode allows this "
                "fallback; profile: production fails startup.",
                UserWarning,
                stacklevel=3,
            )
            budget_llm_mod._unwired_llm_warned = True

    @staticmethod
    def _build_budget_guard_storage(raw: dict[str, Any]) -> BudgetGuardStorage:
        storage_type = raw.get("storage", "memory")
        if storage_type == "memory":
            return InMemoryBudgetGuardStorage()
        if storage_type == "file":
            path = raw.get("path")
            if not path:
                raise ConfigError("budget storage 'file' requires a 'path'")
            return FileBudgetGuardStorage(path)
        if storage_type == "sqlite":
            path = raw.get("path")
            if not path:
                raise ConfigError("budget storage 'sqlite' requires a 'path'")
            return SqliteBudgetGuardStorage(
                path,
                table=str(raw.get("table", "mycelium_budget")),
            )
        if storage_type == "redis":
            from mycelium.storage._helpers import resolve_storage_url

            try:
                url = resolve_storage_url(raw)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            return RedisBudgetGuardStorage(
                url,
                prefix=str(raw.get("prefix", "mycelium:budget:")),
            )
        if storage_type == "postgres":
            from mycelium.storage._helpers import resolve_storage_url

            try:
                dsn = resolve_storage_url(raw, url_key="dsn")
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            return PostgresBudgetGuardStorage(
                dsn,
                table=str(raw.get("table", "mycelium_budget")),
            )
        raise ConfigError(f"unknown budget storage type: {storage_type!r}")

    def build_scope_guard(self) -> ScopeGuard | None:
        """Build a shared ScopeGuard if the config declares ``scope_guard:``."""
        if self.scope_guard is None:
            return None
        if self._scope_guard is not None:
            return self._scope_guard
        raw = self.scope_guard
        shared = self._guard_atomic_backend(raw)
        storage = (
            AtomicScopeGuardStorage(shared[0], namespace=f"{shared[1]}:scope_guard")
            if shared is not None
            else self._build_scope_guard_storage(raw)
        )
        grant = _scope_grant_from_config(
            raw,
            registry_allowed=self.registry_allowed,
            tool_names=list(self.tools.keys()),
        )
        on_violation = raw.get("on_violation", ON_VIOLATION_SOFT)
        if on_violation not in ON_VIOLATION_MODES:
            raise ConfigError(
                f"'scope_guard.on_violation' must be one of {sorted(ON_VIOLATION_MODES)}"
            )
        exclude = raw.get("exclude") or []
        if not isinstance(exclude, list):
            raise ConfigError("'scope_guard.exclude' must be a list of tool names")
        auto_bind = raw.get("auto_bind", True)
        if not isinstance(auto_bind, bool):
            raise ConfigError("'scope_guard.auto_bind' must be a bool")
        missing_run_id_policy = _missing_run_id_policy(
            raw,
            "scope_guard.missing_run_id_policy",
            profile=self.profile,
        )
        self._scope_guard = ScopeGuard(
            storage,
            default_grant=grant,
            on_violation=str(on_violation),
            exclude=[str(item) for item in exclude],
            auto_bind=auto_bind,
            missing_run_id_policy=missing_run_id_policy,
        )
        return self._scope_guard

    @staticmethod
    def _build_scope_guard_storage(raw: dict[str, Any]) -> ScopeGuardStorage:
        storage_type = raw.get("storage", "memory")
        if storage_type == "memory":
            return InMemoryScopeGuardStorage()
        if storage_type == "file":
            path = raw.get("path")
            if not path:
                raise ConfigError("scope_guard storage 'file' requires a 'path'")
            return FileScopeGuardStorage(path)
        raise ConfigError(f"unknown scope_guard storage type: {storage_type!r}")

    @property
    def completion_terminal_wired(self) -> bool:
        """Whether a real terminal adapter was verified for ``completion:``."""
        return bool(self._terminal_adapters)

    def _activate_completion_terminal(self) -> None:
        """Bind the contract and verify a terminal adapter is installed."""
        from mycelium.completion_contract import (
            TERMINAL_ADAPTER_CREWAI,
            TERMINAL_ADAPTER_LANGGRAPH,
        )

        if self.completion is None:
            set_active_completion_contract(None)
            self._terminal_adapters = frozenset()
            return

        contract = self.build_completion_contract()
        set_active_completion_contract(contract)

        installer_path = self.completion.get("adapter_installer")
        if installer_path is not None:
            installer = _import_callable(
                installer_path,
                kind="completion adapter installer",
            )
            try:
                installer()
            except Exception as exc:
                raise ConfigError(
                    f"completion adapter installer {installer_path!r} failed: {exc}"
                ) from exc

        adapters = set(registered_terminal_adapters())
        install_error: str | None = None
        if self.langgraph_enabled:
            try:
                # Resolve through the historical facade so existing host/test
                # monkeypatches of ``mycelium.config`` keep their behavior.
                from mycelium import config as config_facade

                installed = config_facade.install_langgraph_completion_terminal()
            except LangGraphIntegrationError as exc:
                installed = False
                install_error = str(exc)
            if installed:
                adapters.add(TERMINAL_ADAPTER_LANGGRAPH)
        if self.crewai_enabled:
            try:
                installed = self._crewai_runtime_installed or install_crewai_runtime(
                    run_id_from=self.crewai_run_id_from
                )
            except CrewAIIntegrationError as exc:
                installed = False
                install_error = str(exc)
            if installed:
                self._crewai_runtime_installed = True
                adapters.add(TERMINAL_ADAPTER_CREWAI)
        self._terminal_adapters = frozenset(adapters)

        if adapters:
            return
        self._reject_unwired_completion_terminal(install_error)

    def _reject_unwired_completion_terminal(self, install_error: str | None) -> None:
        import mycelium.completion_contract as completion_mod

        selected = [
            name
            for enabled, name in (
                (self.langgraph_enabled, "LangGraph"),
                (self.crewai_enabled, "CrewAI"),
            )
            if enabled
        ]
        framework = " or ".join(selected) if selected else "a supported runtime"
        if install_error:
            detail = f"{framework} terminal adapter was not installed ({install_error})"
        elif not selected:
            detail = (
                "no terminal adapter was explicitly selected. Set "
                "integrations.langgraph.enabled: true or "
                "integrations.crewai.enabled: true (and install that optional "
                "framework extra) so its terminal is protected automatically. "
                "Having LangGraph installed is not enough; neither is having "
                "CrewAI installed"
            )
        else:
            detail = (
                f"no supported terminal path is wired. Enable "
                f"the {framework} integration and install its optional dependency "
                "so the framework terminal is protected automatically"
            )
        manual = (
            "Custom-runtime fallback: set "
            "completion.adapter_installer='package.module:function' to wire "
            "wrap_final_message(...) or gate_graph_end(...) during startup, "
            "or register manually before load_config()."
        )
        if self.profile == PROFILE_PRODUCTION:
            raise ConfigError(
                f"profile is {PROFILE_PRODUCTION!r} and 'completion:' is "
                f"enabled, but {detail}. Completion checks would be bypassed. "
                f"{manual}"
            )
        if not completion_mod._unwired_completion_warned:
            warnings.warn(
                "'completion:' is enabled but no terminal adapter is wired; "
                f"{framework} terminal / final-message paths are not automatically "
                "protected. Enable integrations.langgraph or integrations.crewai, or use "
                "wrap_final_message / gate_graph_end. Development mode allows "
                "this fallback; profile: production fails startup.",
                UserWarning,
                stacklevel=3,
            )
            completion_mod._unwired_completion_warned = True

    def build_completion_contract(self) -> CompletionContract | None:
        """Build a CompletionContract if the config declares ``completion:``."""
        if self.completion is None:
            return None
        if self._completion is not None:
            return self._completion
        raw = self.completion
        shared = self._guard_atomic_backend(raw)
        storage = (
            AtomicCompletionStorage(shared[0], namespace=f"{shared[1]}:completion")
            if shared is not None
            else self._build_completion_storage(raw)
        )
        required, optional = _parse_completion_id_lists(raw)
        self._completion = CompletionContract(
            storage,
            required=required,
            optional=optional,
        )
        return self._completion

    @staticmethod
    def _build_completion_storage(raw: dict[str, Any]) -> CompletionStorage:
        storage_type = raw.get("storage", "memory")
        if storage_type == "memory":
            return InMemoryCompletionStorage()
        if storage_type == "file":
            path = raw.get("path")
            if not path:
                raise ConfigError("completion storage 'file' requires a 'path'")
            return FileCompletionStorage(path)
        raise ConfigError(f"unknown completion storage type: {storage_type!r}")

    def mark_completion(
        self,
        subtask_id: str,
        status: str,
        *,
        reason: str | None = None,
        scope_key: str | None = None,
    ) -> Any:
        """Mark a completion-contract subtask (requires ``completion:`` in YAML)."""
        contract = self.build_completion_contract()
        if contract is None:
            raise ConfigError("no completion: section in config; cannot mark_completion")
        return contract.mark(subtask_id, status, reason=reason, scope_key=scope_key)

    def complete_run(
        self,
        *,
        scope_key: str | None = None,
    ) -> Any:
        """Gate terminal output via AF-007 completion contract."""
        contract = self.build_completion_contract()
        if contract is None:
            raise ConfigError("no completion: section in config; cannot complete_run")
        return contract.complete_run(scope_key=scope_key)

    def build_state_authority(self) -> StateAuthority | None:
        """Build a shared StateAuthority if the config declares ``state_authority:``."""
        if self.state_authority is None:
            return None
        if self._state_authority is not None:
            return self._state_authority
        raw = self.state_authority
        callable_path = raw.get("canonical_callable")
        if not isinstance(callable_path, str) or not callable_path:
            raise ConfigError(
                "'state_authority.canonical_callable' is required "
                "(format: 'package.module:function')"
            )
        parsed = _parse_callable_path(
            callable_path, kind="state_authority", name="canonical_callable"
        )
        assert parsed is not None
        resolver = _import_callable(parsed, kind="state_authority.canonical_callable")
        require_state_ref = bool(raw.get("require_state_ref", False))
        on_mismatch = str(raw.get("on_mismatch", ON_MISMATCH_HARD))
        on_missing = str(raw.get("on_missing", ON_MISMATCH_HARD))
        if on_mismatch not in ON_MISMATCH_MODES:
            raise ConfigError(
                f"'state_authority.on_mismatch' must be one of {sorted(ON_MISMATCH_MODES)}"
            )
        if on_missing not in ON_MISMATCH_MODES:
            raise ConfigError(
                f"'state_authority.on_missing' must be one of {sorted(ON_MISMATCH_MODES)}"
            )
        exclude = raw.get("exclude") or []
        if not isinstance(exclude, list):
            raise ConfigError("'state_authority.exclude' must be a list of tool names")
        agent_id = "state-authority"
        if self.transition is not None and self.transition.agent_id:
            agent_id = self.transition.agent_id
        # Per-tool require override is applied via a thin wrapper authority when
        # needed; global require_state_ref is the default for all wrapped tools.
        require_override = raw.get("require_state_ref")
        if require_override is not None and not isinstance(require_override, bool):
            raise ConfigError("'state_authority.require_state_ref' must be a bool")
        self._state_authority = StateAuthority(
            resolver,
            require_state_ref=require_state_ref,
            on_mismatch=on_mismatch,
            on_missing=on_missing,
            exclude=[str(item) for item in exclude],
            outcome_emitter=self.build_outcome_emitter(),
            agent_id=agent_id,
        )
        return self._state_authority

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
        flush_on_complete = self.state_flush.get("flush_on_complete", True)
        if not isinstance(flush_on_complete, bool):
            raise ConfigError("'state_flush.flush_on_complete' must be a bool")
        shared = self._guard_atomic_backend(self.state_flush)
        storage = (
            AtomicStateFlushStorage(shared[0], namespace=f"{shared[1]}:state_flush")
            if shared is not None
            else self._build_state_flush_storage(self.state_flush)
        )
        flush_on = self.state_flush.get("flush_on")
        if flush_on is not None and not isinstance(flush_on, list):
            raise ConfigError("'state_flush.flush_on' must be a list")
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
                "'audit_receipt.agent_id' is no longer supported; set 'transition.agent_id' instead"
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
        shared = self._guard_atomic_backend(self.audit_receipt)
        storage = (
            AtomicAuditReceiptStorage(shared[0], namespace=f"{shared[1]}:audit_receipt")
            if shared is not None
            else self._build_audit_receipt_storage(self.audit_receipt)
        )
        self._audit_emitter = AuditReceiptEmitter(
            agent_id=str(agent_id),
            signing_key=signing_key,
            storage=storage,
        )
        return self._audit_emitter

    def build_outcome_emitter(self) -> OutcomeEmitter | None:
        """Build an OutcomeEmitter if the config declares one."""
        if self.outcome_emit is None:
            return None
        if self._outcome_emitter is not None:
            return self._outcome_emitter
        agent_id = "mycelium"
        if self.transition is not None:
            agent_id = self.transition.agent_id
        storage = self._build_outcome_storage(self.outcome_emit)
        exporters = self._build_outcome_exporters(self.outcome_emit)
        if exporters:
            storage = FanoutOutcomeStorage(storage, *exporters)
        on_failure = _outcome_on_failure(self.outcome_emit, profile=self.profile)
        self._outcome_emitter = OutcomeEmitter(
            agent_id=str(agent_id),
            storage=storage,
            on_failure=on_failure,
        )
        return self._outcome_emitter

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
            inner: AbstractContextManager[Any] = state_flush.run(run_id, use_session=use_session)
        elif use_session:
            inner = Session()
        else:
            inner = _NoopRun(run_id)
        return _ScopedRunContext(inner, scope)

    def tool_transition_binding(self, tool_config: ToolConfig) -> ToolTransitionBinding | None:
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
            spendability=tool_config.spendability,
            capability=tool_config.capability,
            provider_idempotency_key_param=(tool_config.provider_idempotency_key_param),
            provider_idempotency_key_ttl=(tool_config.provider_idempotency_key_ttl),
            propagate_effect_id_as_provider_key=(tool_config.propagate_effect_id_as_provider_key),
            request_id_from=tool_config.request_id_from,
        )

    def _ledger_timing_kwargs(self) -> dict[str, float | bool]:
        """Return ActionLedger timing and death-signal overrides from ``transition`` config."""
        if self.transition is None:
            return {}
        kwargs: dict[str, float | bool] = {}
        if self.transition.lease_ttl is not None:
            kwargs["lease_ttl"] = self.transition.lease_ttl
        if self.transition.lease_renew_interval is not None:
            kwargs["lease_renew_interval"] = self.transition.lease_renew_interval
        if self.transition.poll_interval is not None:
            kwargs["poll_interval"] = self.transition.poll_interval
        if self.transition.poll_timeout is not None:
            kwargs["poll_timeout"] = self.transition.poll_timeout
        if self.transition.reclaim_requires_death_signal:
            kwargs["reclaim_requires_death_signal"] = True
        if self.transition.presumed_dead_after is not None:
            kwargs["presumed_dead_after"] = self.transition.presumed_dead_after
        return kwargs

    def _tool_audit_emitter(self, tool_config: ToolConfig) -> AuditReceiptEmitter | None:
        if not tool_config.audit_receipt:
            return None
        if tool_config.ledger is None:
            raise ConfigError(f"tool '{tool_config.name}' has audit_receipt enabled but no ledger")
        return self._shared_audit_emitter()

    def _task_audit_emitter(self, task_config: TaskConfig) -> AuditReceiptEmitter | None:
        if not task_config.audit_receipt:
            return None
        if task_config.ledger is None:
            raise ConfigError(f"task '{task_config.name}' has audit_receipt enabled but no ledger")
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
    def _parse_ledger_timing(
        raw: dict[str, Any],
        field: str,
        *,
        default: float | None = None,
        prefix: str = "ledger",
    ) -> float | None:
        if field not in raw:
            return default
        value = raw[field]
        if value is None:
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ConfigError(f"'{prefix}.{field}' must be a finite number > 0")
        return float(value)

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
            ttl = MyceliumConfig._parse_ledger_timing(raw, "in_flight_ttl", default=604800.0)
            retention = MyceliumConfig._parse_ledger_timing(raw, "retention_seconds", default=None)
            return RedisLedgerStorage(
                url,
                prefix=str(raw.get("prefix", "mycelium:action:")),
                in_flight_ttl=ttl,
                retention_seconds=retention,
            )
        if storage_type == "postgres":
            from mycelium.storage._helpers import resolve_storage_url
            from mycelium.storage.postgres_ledger import PostgresLedgerStorage

            pool_sizes = {}
            for field, default in (("pool_min_size", 1), ("pool_max_size", 10)):
                value = raw.get(field, default)
                if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                    raise ConfigError(f"'ledger.{field}' must be an integer >= 1")
                pool_sizes[field] = value
            if pool_sizes["pool_min_size"] > pool_sizes["pool_max_size"]:
                raise ConfigError("'ledger.pool_min_size' must be <= 'ledger.pool_max_size'")
            try:
                dsn = resolve_storage_url(raw, url_key="dsn")
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            retention = MyceliumConfig._parse_ledger_timing(raw, "retention_seconds", default=None)
            return PostgresLedgerStorage(
                dsn,
                table=str(raw.get("table", "mycelium_action_ledger")),
                pool_min_size=pool_sizes["pool_min_size"],
                pool_max_size=pool_sizes["pool_max_size"],
                retention_seconds=retention,
            )
        if storage_type == "sqlite":
            from mycelium.storage.sqlite_ledger import SqliteLedgerStorage

            path = raw.get("path")
            if not path:
                raise ConfigError("ledger storage 'sqlite' requires a 'path'")
            return SqliteLedgerStorage(
                path,
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
            ttl = raw.get("in_flight_ttl", 604800)
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
        if storage_type == "sqlite":
            from mycelium.storage.sqlite_ledger import SqliteTaskLedgerStorage

            path = raw.get("path")
            if not path:
                raise ConfigError("task ledger storage 'sqlite' requires a 'path'")
            return SqliteTaskLedgerStorage(
                path,
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

    @staticmethod
    def _build_outcome_storage(raw: dict[str, Any]) -> OutcomeStorage:
        storage_type = raw.get("storage", "memory")
        if storage_type == "file":
            path = raw.get("path")
            if not path:
                raise ConfigError("outcome_emit storage 'file' requires a 'path'")
            return FileOutcomeStorage(path)
        if storage_type == "memory":
            return InMemoryOutcomeStorage()
        if storage_type == "postgres":
            from mycelium.storage._helpers import resolve_storage_url
            from mycelium.storage.postgres_outcome import PostgresOutcomeStorage

            try:
                dsn = resolve_storage_url(raw, url_key="url", alt_keys=("dsn",))
            except ValueError as exc:
                raise ConfigError(f"outcome_emit storage 'postgres' is incomplete: {exc}") from exc
            table = str(raw.get("table", "mycelium_outcomes"))
            try:
                return PostgresOutcomeStorage(dsn, table=table)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        if storage_type == "redis":
            from mycelium.storage._helpers import resolve_storage_url
            from mycelium.storage.redis_outcome import RedisOutcomeStorage

            try:
                url = resolve_storage_url(raw, url_key="url")
            except ValueError as exc:
                raise ConfigError(f"outcome_emit storage 'redis' is incomplete: {exc}") from exc
            key_prefix = raw.get("key_prefix", raw.get("prefix", "mycelium:outcomes"))
            try:
                return RedisOutcomeStorage(url, key_prefix=str(key_prefix))
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
        raise ConfigError(f"unknown outcome_emit storage type: {storage_type!r}")

    @staticmethod
    def _build_outcome_exporters(raw: dict[str, Any]) -> list[OutcomeStorage]:
        configured = raw.get("exporters", [])
        if not isinstance(configured, list):
            raise ConfigError("'outcome_emit.exporters' must be a list")
        exporters: list[OutcomeStorage] = []
        for index, item in enumerate(configured):
            if not isinstance(item, dict):
                raise ConfigError(f"'outcome_emit.exporters[{index}]' must be a mapping")
            exporter_type = item.get("type")
            try:
                if exporter_type == "opentelemetry":
                    exporters.append(OpenTelemetryOutcomeStorage())
                elif exporter_type == "prometheus":
                    exporters.append(PrometheusOutcomeStorage())
                elif exporter_type == "webhook":
                    from mycelium.storage._helpers import resolve_storage_url

                    url = resolve_storage_url(item, url_key="url")
                    headers = item.get("headers")
                    if headers is not None and not isinstance(headers, dict):
                        raise ConfigError(
                            f"'outcome_emit.exporters[{index}].headers' must be a mapping"
                        )
                    secret = item.get("secret")
                    secret_env = item.get("secret_env")
                    if secret is None and secret_env:
                        secret = os.environ.get(str(secret_env))
                        if not secret:
                            raise ConfigError(f"environment variable {secret_env!r} is not set")
                    exporters.append(
                        WebhookOutcomeStorage(
                            url,
                            headers={str(k): str(v) for k, v in (headers or {}).items()},
                            secret=str(secret) if secret is not None else None,
                            timeout=item.get("timeout", 5.0),
                        )
                    )
                else:
                    raise ConfigError(f"unknown outcome exporter type: {exporter_type!r}")
            except (ImportError, TypeError, ValueError) as exc:
                raise ConfigError(f"outcome exporter {exporter_type!r} is invalid: {exc}") from exc
        return exporters

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


# Preserve the historical qualified name for compatibility and pickling.
MyceliumConfig.__module__ = "mycelium.config"
