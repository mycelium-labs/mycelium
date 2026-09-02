"""YAML configuration loader for Mycelium guards."""

from __future__ import annotations

import functools
import importlib
import inspect
import math
import os
import re
import warnings
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

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
    USE_TIME_CHECKS,
    AuthorityWindowPolicy,
    set_authority_window_policy,
)
from mycelium.budget_guard import (
    MISSING_USAGE_POLICIES,
    MISSING_USAGE_POLICY_ERROR,
    MISSING_USAGE_POLICY_WARN,
    ON_MISSING_HARD,
    ON_MISSING_METER_MODES,
    BudgetCeilings,
    BudgetGuard,
    BudgetGuardStorage,
    FileBudgetGuardStorage,
    InMemoryBudgetGuardStorage,
    PostgresBudgetGuardStorage,
    RedisBudgetGuardStorage,
    SqliteBudgetGuardStorage,
    apply_budget_guard,
    parse_duration_seconds,
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
from mycelium.config_schema import (
    CONFIG_VERSION,
    ToolContractModel,
    config_json_schema,
)
from mycelium.contracts import apply_tool_contract, validate_contract_definition
from mycelium.decision import DecisionPolicyBundle, apply_decision_policy
from mycelium.destructive_confirm import (
    MISSING_POLICIES as DESTRUCTIVE_MISSING_POLICIES,
)
from mycelium.destructive_confirm import (
    MISSING_POLICY_ERROR as DESTRUCTIVE_MISSING_POLICY_ERROR,
)
from mycelium.destructive_confirm import (
    SHARED_GRANT_STORAGES,
    STORAGE_FILE,
    STORAGE_MEMORY,
    STORAGE_POSTGRES,
    STORAGE_REDIS,
    STORAGE_SQLITE,
    DestructiveConfirmPolicy,
    DestructiveGrantSpec,
    DestructiveObjectSpec,
    DestructiveToolPolicy,
    FileDestructiveGrantStore,
    InMemoryDestructiveGrantStore,
    PostgresDestructiveGrantStore,
    RedisDestructiveGrantStore,
    SqliteDestructiveGrantStore,
    apply_destructive_confirm,
    destructive_confirm_policy_for_tool,
)
from mycelium.entity_guard import (
    DEST_TYPES,
    MISSING_POLICIES,
    MISSING_POLICY_ERROR,
    DestinationAllow,
    DestinationSpec,
    EntityGuardPolicy,
    ToolDestinationPolicy,
    apply_entity_guard,
    entity_guard_policy_for_tool,
)
from mycelium.history_guard import HistoryGuard
from mycelium.integrations.langgraph import (
    LangGraphIntegrationError,
    install_langgraph_completion_terminal,
    instrument_langgraph_tool,
)
from mycelium.loop_guard import (
    DEFAULT_CONSECUTIVE_SOFT,
    MISSING_RUN_ID_POLICIES,
    MISSING_RUN_ID_POLICY_ERROR,
    MISSING_RUN_ID_POLICY_WARN,
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
    OUTCOME_ON_FAILURE_ERROR,
    OUTCOME_ON_FAILURE_POLICIES,
    OUTCOME_ON_FAILURE_WARN,
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
    ScopeGrant,
    ScopeGuard,
    ScopeGuardStorage,
    apply_scope_guard,
)
from mycelium.secret_protection import (
    SECRET_ARGS_POLICIES,
    SecretArgsPolicy,
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
    REQUEST_IDENTITY_POLICIES,
    REQUEST_IDENTITY_POLICY_DERIVED,
    REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT,
    RetryPermission,
    SideEffectBoundary,
    SideEffectClass,
    Spendability,
    ToolCapability,
    ToolTransitionBinding,
    TransitionConfig,
    TransitionScope,
    execution_scope,
    parse_capability,
    parse_retry_permission,
    parse_side_effect_boundary,
    parse_side_effect_class,
    parse_spendability,
)
from mycelium.use_time_currency import (
    MISSING_POLICIES as USE_TIME_MISSING_POLICIES,
)
from mycelium.use_time_currency import (
    MISSING_POLICY_ERROR as USE_TIME_MISSING_POLICY_ERROR,
)
from mycelium.use_time_currency import (
    UseTimeCurrencyPolicy,
    UseTimeFactSpec,
    UseTimeToolPolicy,
    apply_use_time_currency,
    set_use_time_currency_policy,
    use_time_currency_policy_for_tool,
)


class ConfigError(Exception):
    """Raised when a Mycelium config file is invalid or inconsistent."""


MEMORY_STORAGE_POLICY_WARN = "warn"
MEMORY_STORAGE_POLICY_ERROR = "error"
MEMORY_STORAGE_POLICIES = frozenset({MEMORY_STORAGE_POLICY_WARN, MEMORY_STORAGE_POLICY_ERROR})

PROFILE_DEVELOPMENT = "development"
PROFILE_PRODUCTION = "production"
PROFILES = frozenset({PROFILE_DEVELOPMENT, PROFILE_PRODUCTION})

# Ledgered tools in these classes must not silently use process-local memory
# storage in production: a restart drops the ledger and dedupe can re-execute.
_SIDE_EFFECTING_MEMORY_CLASSES = frozenset(
    {
        SideEffectClass.IDEMPOTENT_MUTATE,
        SideEffectClass.KEYED_MUTATE,
        SideEffectClass.NON_IDEMPOTENT_MUTATE,
        SideEffectClass.IRREVERSIBLE,
    }
)


_CALLABLE_PATH_RE = re.compile(
    r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*[A-Za-z_][A-Za-z0-9_]*:"
    r"[A-Za-z_][A-Za-z0-9_]*$"
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


def _parse_callable_path(raw: Any, *, kind: str, name: str) -> str | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or not _CALLABLE_PATH_RE.fullmatch(raw):
        raise ConfigError(f"{kind} {name!r}.callable must be 'package.module:function'")
    return raw


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
    spendability: Spendability | None = None
    capability: ToolCapability | None = None
    provider_idempotency_key_param: str | None = None
    provider_idempotency_key_ttl: float | None = None
    propagate_effect_id_as_provider_key: bool = False
    request_id_from: str | None = None
    callable_path: str | None = None
    # Per-tool loop_guard: None=inherit global, False=disable, dict=overrides
    loop_guard: dict[str, Any] | bool | None = None
    # Per-tool budget_guard: None=inherit global, False=disable
    budget_guard: bool | None = None
    # Per-tool scope_guard: None=inherit global, False=disable, dict=overrides
    scope_guard: dict[str, Any] | bool | None = None
    # Per-tool state_authority: None=inherit global, False=disable, dict=overrides
    state_authority: dict[str, Any] | bool | None = None
    # Fields that may hold secret:// references (resolved only at execution).
    secret_fields: tuple[str, ...] = ()
    # Per-tool secret_args: None=inherit global, False=disable
    secret_args: bool | None = None
    # Per-tool entity_guard: None=inherit global, False=disable
    entity_guard: bool | None = None
    # Per-tool destructive_confirm: None=inherit global, False=disable
    destructive_confirm: bool | None = None
    # Per-tool use_time_currency: None=inherit global, False=disable
    use_time_currency: bool | None = None
    contract: ToolContractModel | None = None

    def is_noop(self) -> bool:
        return (
            self.protect is None
            and self.bounded is None
            and self.ledger is None
            and not self.audit_receipt
            and self.loop_guard is None
            and self.budget_guard is None
            and self.scope_guard is None
            and self.state_authority is None
            and not self.secret_fields
            and self.secret_args is None
            and self.entity_guard is None
            and self.destructive_confirm is None
            and self.use_time_currency is None
            and self.contract is None
        )


@dataclass(frozen=True)
class TaskConfig:
    """Parsed configuration for a single task."""

    name: str
    ledger: dict[str, Any] | None = None
    audit_receipt: bool = False
    callable_path: str | None = None

    def is_noop(self) -> bool:
        return self.ledger is None and not self.audit_receipt


@dataclass(frozen=True)
class AutoInstrumentationTarget:
    """A YAML entry resolved by command-based auto-instrumentation."""

    kind: str
    name: str
    callable_path: str


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

        # LangGraph outermost so it can inject scope/dispatch before inner guards.
        if self.langgraph_enabled and (
            tool_config.ledger is not None
            or applies_loop
            or applies_scope
            or applies_state
            or applies_secret
            or applies_entity
            or applies_destructive
            or applies_use_time
        ):
            try:
                func = instrument_langgraph_tool(func)
            except LangGraphIntegrationError as exc:
                raise ConfigError(str(exc)) from exc

        _mark_config_applied(func, kind="tool", name=name)
        return func

    @property
    def langgraph_enabled(self) -> bool:
        """Whether automatic LangGraph ToolRuntime identity is enabled."""
        if self.integrations is None:
            return False
        return bool(self.integrations.get("langgraph", {}).get("enabled", False))

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
        try:
            warn_at_f = float(warn_at)
        except (TypeError, ValueError) as exc:
            raise ConfigError("'budget.warn_at' must be a float in (0, 1]") from exc
        if not 0.0 < warn_at_f <= 1.0:
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
        from mycelium.completion_contract import TERMINAL_ADAPTER_LANGGRAPH

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
                installed = install_langgraph_completion_terminal()
            except LangGraphIntegrationError as exc:
                installed = False
                install_error = str(exc)
            if installed:
                adapters.add(TERMINAL_ADAPTER_LANGGRAPH)
        self._terminal_adapters = frozenset(adapters)

        if adapters:
            return
        self._reject_unwired_completion_terminal(install_error)

    def _reject_unwired_completion_terminal(self, install_error: str | None) -> None:
        import mycelium.completion_contract as completion_mod

        framework = "LangGraph"
        if install_error:
            detail = f"{framework} terminal adapter was not installed ({install_error})"
        elif not self.langgraph_enabled:
            detail = (
                f"no terminal adapter was explicitly selected. Set "
                f"integrations.langgraph.enabled: true (and install "
                f"'mycelium-runtime[langgraph]') so {framework} END is "
                f"protected automatically. Having LangGraph installed is "
                f"not enough"
            )
        else:
            detail = (
                f"no supported terminal path is wired. Enable "
                f"integrations.langgraph (install 'mycelium-runtime[langgraph]') "
                f"so {framework} END is protected automatically"
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
                f"{framework} END / final-message paths are not automatically "
                "protected. Enable integrations.langgraph or use "
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
        shared = self._guard_atomic_backend(self.state_flush)
        storage = (
            AtomicStateFlushStorage(shared[0], namespace=f"{shared[1]}:state_flush")
            if shared is not None
            else self._build_state_flush_storage(self.state_flush)
        )
        flush_on = self.state_flush.get("flush_on")
        if flush_on is not None and not isinstance(flush_on, list):
            raise ConfigError("'state_flush.flush_on' must be a list")
        flush_on_complete = _parse_bool_option(
            self.state_flush,
            "flush_on_complete",
            field="'state_flush.flush_on_complete'",
            default=True,
        )
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
            ttl = raw.get("in_flight_ttl", 604800)
            retention = raw.get("retention_seconds")
            return RedisLedgerStorage(
                url,
                prefix=str(raw.get("prefix", "mycelium:action:")),
                in_flight_ttl=float(ttl) if ttl is not None else None,
                retention_seconds=float(retention) if retention is not None else None,
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
                pool_min_size=int(raw.get("pool_min_size", 1)),
                pool_max_size=int(raw.get("pool_max_size", 10)),
                retention_seconds=(
                    float(raw["retention_seconds"])
                    if raw.get("retention_seconds") is not None
                    else None
                ),
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


def _budget_ceilings_from_config(raw: dict[str, Any]) -> BudgetCeilings:
    """Parse ``max_duration`` / ``max_steps`` / ``max_tokens`` / ``max_usd``."""
    max_duration_raw = raw.get("max_duration")
    max_steps_raw = raw.get("max_steps")
    max_tokens_raw = raw.get("max_tokens")
    max_usd_raw = raw.get("max_usd")
    max_cost_raw = raw.get("max_cost_usd")
    max_duration: float | None = None
    max_steps: int | None = None
    max_tokens: int | None = None
    max_usd: float | None = None
    if max_duration_raw is not None:
        try:
            max_duration = parse_duration_seconds(max_duration_raw)
        except ValueError as exc:
            raise ConfigError(f"'budget.max_duration': {exc}") from exc
    if max_steps_raw is not None:
        if not isinstance(max_steps_raw, int) or isinstance(max_steps_raw, bool):
            raise ConfigError("'budget.max_steps' must be a positive int")
        max_steps = max_steps_raw
    if max_tokens_raw is not None:
        if not isinstance(max_tokens_raw, int) or isinstance(max_tokens_raw, bool):
            raise ConfigError("'budget.max_tokens' must be a positive int")
        max_tokens = max_tokens_raw
    parsed_usd: float | None = None
    parsed_cost: float | None = None
    if max_usd_raw is not None:
        try:
            parsed_usd = float(max_usd_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError("'budget.max_usd' must be a positive number") from exc
    if max_cost_raw is not None:
        try:
            parsed_cost = float(max_cost_raw)
        except (TypeError, ValueError) as exc:
            raise ConfigError("'budget.max_cost_usd' must be a positive number") from exc
    if parsed_usd is not None and parsed_cost is not None and parsed_usd != parsed_cost:
        raise ConfigError("'budget.max_usd' and 'budget.max_cost_usd' disagree; use one")
    max_usd = parsed_usd if parsed_usd is not None else parsed_cost
    try:
        return BudgetCeilings(
            max_duration=max_duration,
            max_steps=max_steps,
            max_tokens=max_tokens,
            max_usd=max_usd,
        )
    except ValueError as exc:
        raise ConfigError(f"budget: {exc}") from exc


def _missing_usage_policy(
    raw: dict[str, Any] | None,
    *,
    profile: str = PROFILE_DEVELOPMENT,
) -> str:
    """Return ``missing_usage_policy``, defaulting to ``warn``.

    ``profile: production`` with token/cost limits treats an omitted policy
    as ``error``. An explicit ``warn`` is rejected so production cannot be
    silently weakened.
    """
    ceilings = _budget_ceilings_from_config(raw or {})
    token_or_cost = ceilings.requires_usage_meter()
    if raw is None:
        return MISSING_USAGE_POLICY_WARN
    if "missing_usage_policy" in raw:
        value = raw["missing_usage_policy"]
        if value not in MISSING_USAGE_POLICIES:
            raise ConfigError(
                f"'budget.missing_usage_policy' must be "
                f"{MISSING_USAGE_POLICY_WARN!r} or "
                f"{MISSING_USAGE_POLICY_ERROR!r}, got {value!r}"
            )
        if profile == PROFILE_PRODUCTION and token_or_cost and value == MISSING_USAGE_POLICY_WARN:
            _reject_weaker_production_policy("budget.missing_usage_policy", str(value))
        return str(value)
    if profile == PROFILE_PRODUCTION and token_or_cost:
        return MISSING_USAGE_POLICY_ERROR
    return MISSING_USAGE_POLICY_WARN


def _storage_settings(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Strip integration-only keys from a global ledger/flush section."""
    if cfg is None:
        return {"storage": "memory"}
    return {
        key: value
        for key, value in cfg.items()
        if key not in ("tools", "tasks", "auto", "memory_storage_policy")
    }


def _merge_storage_settings(
    base: dict[str, Any] | None,
    override: dict[str, Any],
) -> dict[str, Any]:
    merged = dict(_storage_settings(base))
    merged.update(override)
    return merged


def _parse_bool_option(
    raw: dict[str, Any],
    key: str,
    *,
    field: str,
    default: bool,
) -> bool:
    if key not in raw:
        return default
    value = raw[key]
    if not isinstance(value, bool):
        raise ConfigError(f"{field} must be a boolean")
    return value


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
    audit_receipt = _parse_bool_option(
        raw,
        "audit_receipt",
        field=f"tool '{name}'.audit_receipt",
        default=False,
    )

    if protect is not None and not isinstance(protect, dict):
        raise ConfigError(f"tool '{name}'.protect must be a mapping")
    if bounded is not None and not isinstance(bounded, dict):
        raise ConfigError(f"tool '{name}'.bounded must be a mapping")

    contract_keys = (
        "operations",
        "required_args",
        "optional_args",
        "argument_types",
        "output_schema",
        "capabilities",
    )
    contract_raw = raw.get("contract")
    direct_contract = {key: raw[key] for key in contract_keys if key in raw}
    if contract_raw is not None and direct_contract:
        raise ConfigError(
            f"tool '{name}': use either contract: or direct contract fields, not both"
        )
    if contract_raw is not None and not isinstance(contract_raw, dict):
        raise ConfigError(f"tool '{name}'.contract must be a mapping")
    contract = None
    if contract_raw is not None or direct_contract:
        try:
            contract = ToolContractModel.model_validate(
                contract_raw if contract_raw is not None else direct_contract
            )
            validate_contract_definition(contract, tool_name=name)
        except (ValueError, TypeError) as exc:
            raise ConfigError(str(exc)) from exc

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
            side_effect_boundary = parse_side_effect_boundary(raw["side_effect_boundary"])
        except ValueError as exc:
            raise ConfigError(f"tool '{name}': {exc}") from exc

    spendability: Spendability | None = None
    if "spendability" in raw:
        try:
            spendability = parse_spendability(raw["spendability"])
        except ValueError as exc:
            raise ConfigError(f"tool '{name}': {exc}") from exc

    capability: ToolCapability | None = None
    if "capability" in raw:
        try:
            capability = parse_capability(raw["capability"])
        except ValueError as exc:
            raise ConfigError(f"tool '{name}': {exc}") from exc

    provider_idempotency_key_param: str | None = None
    if "provider_idempotency_key_param" in raw:
        value = raw["provider_idempotency_key_param"]
        if not isinstance(value, str):
            raise ConfigError(f"tool '{name}': provider_idempotency_key_param must be a string")
        provider_idempotency_key_param = value

    provider_idempotency_key_ttl: float | None = None
    if "provider_idempotency_key_ttl" in raw:
        value = raw["provider_idempotency_key_ttl"]
        if not isinstance(value, (int, float)) or value <= 0:
            raise ConfigError(
                f"tool '{name}': provider_idempotency_key_ttl must be a positive number"
            )
        provider_idempotency_key_ttl = float(value)

    propagate_effect_id_as_provider_key = False
    if "propagate_effect_id_as_provider_key" in raw:
        value = raw["propagate_effect_id_as_provider_key"]
        if not isinstance(value, bool):
            raise ConfigError(f"tool '{name}': propagate_effect_id_as_provider_key must be a bool")
        propagate_effect_id_as_provider_key = value
    if propagate_effect_id_as_provider_key and provider_idempotency_key_param is None:
        raise ConfigError(
            f"tool '{name}': propagate_effect_id_as_provider_key requires "
            "provider_idempotency_key_param"
        )

    request_id_from: str | None = None
    if "request_id_from" in raw:
        value = raw["request_id_from"]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"tool '{name}': request_id_from must be a non-empty string "
                "naming a server-owned business argument"
            )
        request_id_from = value.strip()

    callable_path = _parse_callable_path(
        raw.get("callable"),
        kind="tool",
        name=name,
    )

    loop_guard_raw = raw.get("loop_guard")
    loop_guard_cfg: dict[str, Any] | bool | None
    if loop_guard_raw is None:
        loop_guard_cfg = None
    elif loop_guard_raw is False:
        loop_guard_cfg = False
    elif loop_guard_raw is True:
        loop_guard_cfg = {}
    elif isinstance(loop_guard_raw, dict):
        loop_guard_cfg = loop_guard_raw
    else:
        raise ConfigError(f"tool '{name}'.loop_guard must be a bool or a mapping")

    budget_guard_raw = raw.get("budget_guard")
    budget_guard_cfg: bool | None
    if budget_guard_raw is None:
        budget_guard_cfg = None
    elif isinstance(budget_guard_raw, bool):
        budget_guard_cfg = budget_guard_raw
    else:
        raise ConfigError(f"tool '{name}'.budget_guard must be a bool")

    scope_guard_raw = raw.get("scope_guard")
    scope_guard_cfg: dict[str, Any] | bool | None
    if scope_guard_raw is None:
        scope_guard_cfg = None
    elif scope_guard_raw is False:
        scope_guard_cfg = False
    elif scope_guard_raw is True:
        scope_guard_cfg = {}
    elif isinstance(scope_guard_raw, dict):
        scope_guard_cfg = scope_guard_raw
    else:
        raise ConfigError(f"tool '{name}'.scope_guard must be a bool or a mapping")

    state_authority_raw = raw.get("state_authority")
    state_authority_cfg: dict[str, Any] | bool | None
    if state_authority_raw is None:
        state_authority_cfg = None
    elif state_authority_raw is False:
        state_authority_cfg = False
    elif state_authority_raw is True:
        state_authority_cfg = {}
    elif isinstance(state_authority_raw, dict):
        state_authority_cfg = state_authority_raw
    else:
        raise ConfigError(f"tool '{name}'.state_authority must be a bool or a mapping")

    secret_fields_raw = raw.get("secret_fields")
    secret_fields: tuple[str, ...] = ()
    if secret_fields_raw is not None:
        if not isinstance(secret_fields_raw, list) or not all(
            isinstance(item, str) and item.strip() for item in secret_fields_raw
        ):
            raise ConfigError(f"tool '{name}'.secret_fields must be a list of non-empty strings")
        secret_fields = tuple(item.strip() for item in secret_fields_raw)

    secret_args_raw = raw.get("secret_args")
    secret_args_cfg: bool | None
    if secret_args_raw is None:
        secret_args_cfg = None
    elif isinstance(secret_args_raw, bool):
        secret_args_cfg = secret_args_raw
    else:
        raise ConfigError(f"tool '{name}'.secret_args must be a bool")

    entity_guard_raw = raw.get("entity_guard")
    entity_guard_cfg: bool | None
    if entity_guard_raw is None:
        entity_guard_cfg = None
    elif isinstance(entity_guard_raw, bool):
        entity_guard_cfg = entity_guard_raw
    else:
        raise ConfigError(f"tool '{name}'.entity_guard must be a bool")

    destructive_raw = raw.get("destructive_confirm")
    destructive_cfg: bool | None
    if destructive_raw is None:
        destructive_cfg = None
    elif isinstance(destructive_raw, bool):
        destructive_cfg = destructive_raw
    else:
        raise ConfigError(f"tool '{name}'.destructive_confirm must be a bool")

    use_time_raw = raw.get("use_time_currency")
    use_time_cfg: bool | None
    if use_time_raw is None:
        use_time_cfg = None
    elif isinstance(use_time_raw, bool):
        use_time_cfg = use_time_raw
    else:
        raise ConfigError(f"tool '{name}'.use_time_currency must be a bool")

    return ToolConfig(
        name=name,
        protect=protect,
        bounded=bounded,
        ledger=ledger,
        audit_receipt=audit_receipt,
        side_effect_class=side_effect_class,
        retry_permission=retry_permission,
        side_effect_boundary=side_effect_boundary,
        spendability=spendability,
        capability=capability,
        provider_idempotency_key_param=provider_idempotency_key_param,
        provider_idempotency_key_ttl=provider_idempotency_key_ttl,
        propagate_effect_id_as_provider_key=propagate_effect_id_as_provider_key,
        request_id_from=request_id_from,
        callable_path=callable_path,
        loop_guard=loop_guard_cfg,
        budget_guard=budget_guard_cfg,
        scope_guard=scope_guard_cfg,
        state_authority=state_authority_cfg,
        secret_fields=secret_fields,
        secret_args=secret_args_cfg,
        entity_guard=entity_guard_cfg,
        destructive_confirm=destructive_cfg,
        use_time_currency=use_time_cfg,
        contract=contract,
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
    audit_receipt = _parse_bool_option(
        raw,
        "audit_receipt",
        field=f"task '{name}'.audit_receipt",
        default=False,
    )
    ledger = _normalize_ledger_config(name, ledger_raw, task_ledger_global)
    if audit_auto and ledger is not None and raw.get("audit_receipt") is not False:
        audit_receipt = True

    callable_path = _parse_callable_path(
        raw.get("callable"),
        kind="task",
        name=name,
    )
    return TaskConfig(
        name=name,
        ledger=ledger,
        audit_receipt=audit_receipt,
        callable_path=callable_path,
    )


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
            spendability=existing.spendability,
            provider_idempotency_key_param=existing.provider_idempotency_key_param,
            provider_idempotency_key_ttl=existing.provider_idempotency_key_ttl,
            propagate_effect_id_as_provider_key=existing.propagate_effect_id_as_provider_key,
            request_id_from=existing.request_id_from,
            callable_path=existing.callable_path,
            loop_guard=existing.loop_guard,
            budget_guard=existing.budget_guard,
            scope_guard=existing.scope_guard,
            state_authority=existing.state_authority,
            secret_fields=existing.secret_fields,
            secret_args=existing.secret_args,
            entity_guard=existing.entity_guard,
            destructive_confirm=existing.destructive_confirm,
            use_time_currency=existing.use_time_currency,
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
            callable_path=existing.callable_path,
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


def _parse_optional_non_negative_float(
    raw: dict[str, Any],
    key: str,
    *,
    section: str,
    allow_null: bool = False,
) -> float | None:
    """Like positive float, but ``0`` is allowed (e.g. disable lease auto-renew)."""
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
    if parsed < 0:
        raise ConfigError(f"'{section}.{key}' must be greater than or equal to zero")
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

    lease_ttl = _parse_optional_positive_float(raw, "lease_ttl", section="transition")
    lease_renew_interval = _parse_optional_non_negative_float(
        raw, "lease_renew_interval", section="transition"
    )
    poll_interval = _parse_optional_positive_float(raw, "poll_interval", section="transition")
    poll_timeout = _parse_optional_positive_float(raw, "poll_timeout", section="transition")

    reclaim_requires_death_signal = bool(raw.get("reclaim_requires_death_signal", True))
    presumed_dead_after = _parse_optional_positive_float(
        raw, "presumed_dead_after", section="transition"
    )

    return TransitionConfig(
        agent_id=str(agent_id),
        policy_version=str(policy_version),
        scope_from=scope_from,
        lease_ttl=lease_ttl,
        lease_renew_interval=lease_renew_interval,
        poll_interval=poll_interval,
        poll_timeout=poll_timeout,
        reclaim_requires_death_signal=reclaim_requires_death_signal,
        presumed_dead_after=presumed_dead_after,
    )


def _parse_completion_id_lists(raw: dict[str, Any]) -> tuple[list[str], list[str]]:
    """Parse ``completion.required`` / ``completion.optional`` id lists."""

    def _ids(key: str) -> list[str]:
        items = raw.get(key) or []
        if not isinstance(items, list):
            raise ConfigError(f"'completion.{key}' must be a list")
        out: list[str] = []
        for i, item in enumerate(items):
            if isinstance(item, str):
                sid = item.strip()
            elif isinstance(item, dict):
                sid = str(item.get("id", "")).strip()
            else:
                raise ConfigError(
                    f"'completion.{key}[{i}]' must be a string id or {{id: ...}} mapping"
                )
            if not sid:
                raise ConfigError(f"'completion.{key}[{i}]' missing id")
            out.append(sid)
        return out

    required = _ids("required")
    optional = _ids("optional")
    overlap = set(required) & set(optional)
    if overlap:
        raise ConfigError(f"completion ids cannot be both required and optional: {sorted(overlap)}")
    if not required and not optional:
        raise ConfigError("'completion' needs at least one id under required: or optional:")
    return required, optional


def _scope_grant_from_config(
    raw: dict[str, Any],
    *,
    registry_allowed: list[str],
    tool_names: list[str] | None = None,
) -> ScopeGrant:
    """Build the frozen default allowlist from YAML ``scope_guard:`` keys."""
    allowed_raw = raw.get("allowed_tools", "from_registry")
    if allowed_raw == "from_registry":
        allowed = [str(t) for t in registry_allowed]
        if not allowed and tool_names:
            allowed = [str(t) for t in tool_names]
    elif allowed_raw == "all":
        names = tool_names if tool_names is not None else list(registry_allowed)
        allowed = [str(t) for t in names]
    elif isinstance(allowed_raw, list):
        allowed = [str(t) for t in allowed_raw]
    else:
        raise ConfigError(
            "'scope_guard.allowed_tools' must be 'from_registry', 'all', or a list of tool names"
        )
    if not allowed:
        raise ConfigError(
            "'scope_guard' needs a non-empty allowlist: set allowed_tools, "
            "registry.allowed / registry.auto, or tools:"
        )
    return ScopeGrant(allowed_tools=frozenset(allowed))


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


def _parse_profile(data: dict[str, Any]) -> str:
    """Return the config profile, defaulting to ``development``."""
    value = data.get("profile", PROFILE_DEVELOPMENT)
    if value not in PROFILES:
        raise ConfigError(
            f"'profile' must be {PROFILE_DEVELOPMENT!r} or {PROFILE_PRODUCTION!r}, got {value!r}"
        )
    return str(value)


def _reject_weaker_production_policy(field_path: str, value: str) -> None:
    raise ConfigError(
        f"profile is {PROFILE_PRODUCTION!r} but '{field_path}' is {value!r}; "
        f"production requires 'error' and will not silently weaken to 'warn'. "
        f"Remove '{field_path}' or set it to 'error'."
    )


def _missing_run_id_policy(
    raw: dict[str, Any] | None,
    field_path: str,
    *,
    profile: str = PROFILE_DEVELOPMENT,
) -> str:
    """Return ``missing_run_id_policy``, defaulting to ``warn``.

    ``profile: production`` treats an omitted policy as ``error`` for an
    enabled guard. An explicit ``warn`` is rejected so production cannot be
    silently weakened.
    """
    if raw is None:
        return MISSING_RUN_ID_POLICY_WARN
    if "missing_run_id_policy" in raw:
        value = raw["missing_run_id_policy"]
        if value not in MISSING_RUN_ID_POLICIES:
            raise ConfigError(
                f"'{field_path}' must be {MISSING_RUN_ID_POLICY_WARN!r} or "
                f"{MISSING_RUN_ID_POLICY_ERROR!r}, got {value!r}"
            )
        if profile == PROFILE_PRODUCTION and value == MISSING_RUN_ID_POLICY_WARN:
            _reject_weaker_production_policy(field_path, str(value))
        return str(value)
    if profile == PROFILE_PRODUCTION:
        return MISSING_RUN_ID_POLICY_ERROR
    return MISSING_RUN_ID_POLICY_WARN


def _memory_storage_policy(
    action_ledger: dict[str, Any] | None,
    *,
    profile: str = PROFILE_DEVELOPMENT,
) -> str:
    """Return the configured memory-storage policy, defaulting to ``warn``.

    ``profile: production`` treats an omitted policy as ``error``. An explicit
    ``warn`` is rejected so production cannot be silently weakened.
    """
    if action_ledger is not None and "memory_storage_policy" in action_ledger:
        raw = action_ledger["memory_storage_policy"]
        if raw not in MEMORY_STORAGE_POLICIES:
            raise ConfigError(
                "'action_ledger.memory_storage_policy' must be "
                f"{MEMORY_STORAGE_POLICY_WARN!r} or "
                f"{MEMORY_STORAGE_POLICY_ERROR!r}, got {raw!r}"
            )
        if profile == PROFILE_PRODUCTION and raw == MEMORY_STORAGE_POLICY_WARN:
            _reject_weaker_production_policy(
                "action_ledger.memory_storage_policy",
                str(raw),
            )
        return str(raw)
    if profile == PROFILE_PRODUCTION:
        return MEMORY_STORAGE_POLICY_ERROR
    return MEMORY_STORAGE_POLICY_WARN


def _request_identity_policy(
    action_ledger: dict[str, Any] | None,
    *,
    profile: str = PROFILE_DEVELOPMENT,
) -> str:
    """Return ``request_identity_policy``, defaulting to ``derived``.

    ``profile: production`` treats an omitted policy as ``require_explicit``.
    An explicit ``derived`` is rejected so production cannot be silently
    weakened.
    """
    if action_ledger is not None and "request_identity_policy" in action_ledger:
        raw = action_ledger["request_identity_policy"]
        if raw not in REQUEST_IDENTITY_POLICIES:
            raise ConfigError(
                "'action_ledger.request_identity_policy' must be "
                f"{REQUEST_IDENTITY_POLICY_DERIVED!r} or "
                f"{REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT!r}, got {raw!r}"
            )
        if profile == PROFILE_PRODUCTION and raw == REQUEST_IDENTITY_POLICY_DERIVED:
            raise ConfigError(
                f"profile is {PROFILE_PRODUCTION!r} but "
                "'action_ledger.request_identity_policy' is "
                f"{REQUEST_IDENTITY_POLICY_DERIVED!r}; production requires "
                f"{REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT!r} and will not "
                "silently weaken. Remove it or set it to "
                f"{REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT!r}."
            )
        return str(raw)
    if profile == PROFILE_PRODUCTION:
        return REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT
    return REQUEST_IDENTITY_POLICY_DERIVED


def _outcome_on_failure(
    outcome_emit: dict[str, Any] | None,
    *,
    profile: str = PROFILE_DEVELOPMENT,
) -> str:
    if outcome_emit is not None and "on_failure" in outcome_emit:
        raw = outcome_emit["on_failure"]
        if raw not in OUTCOME_ON_FAILURE_POLICIES:
            raise ConfigError(
                "'outcome_emit.on_failure' must be "
                f"{OUTCOME_ON_FAILURE_WARN!r} or "
                f"{OUTCOME_ON_FAILURE_ERROR!r}, got {raw!r}"
            )
        if profile == PROFILE_PRODUCTION and raw == OUTCOME_ON_FAILURE_WARN:
            raise ConfigError(
                f"profile is {PROFILE_PRODUCTION!r} but "
                "'outcome_emit.on_failure' is 'warn'; production requires "
                "'error' and will not silently weaken. Remove it or set it "
                "to 'error'."
            )
        return str(raw)
    if profile == PROFILE_PRODUCTION:
        return OUTCOME_ON_FAILURE_ERROR
    return OUTCOME_ON_FAILURE_WARN


def _enforce_production_outcome_emit(
    outcome_emit: dict[str, Any] | None,
    *,
    profile: str,
) -> None:
    """Production must declare durable outcome emission."""
    if profile != PROFILE_PRODUCTION:
        return
    if outcome_emit is None:
        raise ConfigError(
            f"profile is {PROFILE_PRODUCTION!r} but 'outcome_emit:' is "
            "missing. Production requires durable, machine-readable "
            "decision evidence. Add outcome_emit with storage: "
            "postgres (recommended for distributed), redis "
            "(with persistence: required), or file (single-node)."
        )
    storage_type = outcome_emit.get("storage", "memory")
    if storage_type == "memory":
        raise ConfigError(
            f"profile is {PROFILE_PRODUCTION!r} but outcome_emit uses "
            "memory storage. Production requires a durable backend: "
            "postgres (recommended), redis with persistence: required, "
            "or file (single-node only)."
        )
    if storage_type == "file":
        if not outcome_emit.get("path"):
            raise ConfigError("outcome_emit storage 'file' requires a 'path'")
    elif storage_type == "postgres":
        from mycelium.storage._helpers import resolve_storage_url

        try:
            resolve_storage_url(outcome_emit, url_key="url", alt_keys=("dsn",))
        except ValueError as exc:
            raise ConfigError(f"outcome_emit storage 'postgres' is incomplete: {exc}") from exc
        table = outcome_emit.get("table", "mycelium_outcomes")
        if not isinstance(table, str) or not table:
            raise ConfigError("outcome_emit storage 'postgres' table must be a non-empty string")
    elif storage_type == "redis":
        from mycelium.storage._helpers import resolve_storage_url

        try:
            resolve_storage_url(outcome_emit, url_key="url")
        except ValueError as exc:
            raise ConfigError(f"outcome_emit storage 'redis' is incomplete: {exc}") from exc
        persistence = outcome_emit.get("persistence")
        if persistence != "required":
            raise ConfigError(
                f"profile is {PROFILE_PRODUCTION!r} but outcome_emit "
                "storage is redis without persistence: required. Redis is "
                "only accepted as production-durable when you explicitly "
                "acknowledge that AOF (or an equivalently durable Redis "
                "deployment) is enabled. Mycelium cannot independently "
                "verify the server's persistence configuration."
            )
    else:
        raise ConfigError(
            f"unknown outcome_emit storage type for production: "
            f"{storage_type!r}. Use storage: postgres, redis "
            "(with persistence: required), or file (single-node)."
        )
    _outcome_on_failure(outcome_emit, profile=profile)


def _side_effecting_memory_tools(
    tools: dict[str, ToolConfig],
) -> list[tuple[str, SideEffectClass]]:
    """Ledgered mutating tools whose storage is process-local memory."""
    affected: list[tuple[str, SideEffectClass]] = []
    for name, tool in tools.items():
        if tool.ledger is None or tool.side_effect_class is None:
            continue
        if tool.side_effect_class not in _SIDE_EFFECTING_MEMORY_CLASSES:
            continue
        storage_type = tool.ledger.get("storage", "memory")
        if storage_type != "memory":
            continue
        affected.append((name, tool.side_effect_class))
    return affected


def _enforce_memory_storage_policy(
    tools: dict[str, ToolConfig],
    transition: TransitionConfig | None,
    action_ledger: dict[str, Any] | None,
    *,
    profile: str = PROFILE_DEVELOPMENT,
) -> None:
    """Apply ``action_ledger.memory_storage_policy`` at YAML load time.

    ``storage: memory`` stays available for tests and local development.
    ``warn`` (default) emits a one-time warning per side-effecting tool when
    ``transition:`` is configured — the duplicate-side-effect guard only holds
    within the process. ``error`` rejects those tools with :class:`ConfigError`
    so production cannot silently lose ledger state across a restart. Reads
    may keep using memory storage under either policy. ``profile: production``
    applies ``error`` unless the user already set it.
    """
    policy = _memory_storage_policy(action_ledger, profile=profile)
    affected = _side_effecting_memory_tools(tools)
    if not affected:
        return

    if policy == MEMORY_STORAGE_POLICY_ERROR:
        names = ", ".join(repr(name) for name, _ in affected)
        classes = ", ".join(sorted({cls.value for _, cls in affected}))
        verb = "is" if len(affected) == 1 else "are"
        noun = "tool" if len(affected) == 1 else "tools"
        raise ConfigError(
            f"{noun} {names} {verb} side-effecting ({classes}) but the "
            "action ledger uses memory storage; memory_storage_policy is "
            "'error'. Use file/sqlite/redis/postgres so ledger state "
            "survives a process restart."
        )

    if transition is None:
        return
    for name, side_effect_class in affected:
        warnings.warn(
            f"tool {name!r} is side-effecting ({side_effect_class.value}) "
            "but its ledger uses memory storage; the duplicate-side-effect "
            "guard only holds within this process. Use file/sqlite/redis/postgres "
            "for production deployments.",
            stacklevel=1,
        )


def _validate_callable_targets(
    tools: dict[str, ToolConfig],
    tasks: dict[str, TaskConfig],
) -> None:
    seen: dict[str, tuple[str, str]] = {}
    entries = [
        *((tool.callable_path, "tool", name) for name, tool in tools.items()),
        *((task.callable_path, "task", name) for name, task in tasks.items()),
    ]
    for callable_path, kind, name in entries:
        if callable_path is None:
            continue
        previous = seen.get(callable_path)
        if previous is not None:
            raise ConfigError(
                f"callable {callable_path!r} is configured more than once: "
                f"{previous[0]} {previous[1]!r} and {kind} {name!r}"
            )
        seen[callable_path] = (kind, name)


def _parse_integrations(data: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    raw = data.get("integrations")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'integrations' must be a mapping")

    unknown = set(raw) - {"langgraph"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ConfigError(f"unsupported integration(s): {names}")

    langgraph_raw = raw.get("langgraph")
    if langgraph_raw is None:
        return {}
    if isinstance(langgraph_raw, bool):
        return {"langgraph": {"enabled": langgraph_raw}}
    if not isinstance(langgraph_raw, dict):
        raise ConfigError("'integrations.langgraph' must be a mapping or boolean")

    unknown_langgraph = set(langgraph_raw) - {"enabled"}
    if unknown_langgraph:
        names = ", ".join(sorted(str(name) for name in unknown_langgraph))
        raise ConfigError(f"unsupported 'integrations.langgraph' option(s): {names}")
    enabled = langgraph_raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("'integrations.langgraph.enabled' must be a boolean")
    return {"langgraph": {"enabled": enabled}}


_DEPLOYMENT_TOPOLOGIES = frozenset({"single_node", "multi_node"})


def _parse_deployment(data: dict[str, Any]) -> dict[str, Any] | None:
    """Optional deployment topology hint for ``mycelium doctor``."""
    raw = data.get("deployment")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'deployment' must be a mapping")
    unknown = set(raw) - {"topology"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ConfigError(f"unsupported 'deployment' option(s): {names}")
    if "topology" not in raw:
        return {}
    topology = raw["topology"]
    if topology not in _DEPLOYMENT_TOPOLOGIES:
        raise ConfigError(
            f"'deployment.topology' must be 'single_node' or 'multi_node', got {topology!r}"
        )
    return {"topology": str(topology)}


def _parse_verify(data: dict[str, Any]) -> dict[str, Any] | None:
    """Optional isolation settings for ``mycelium verify``."""
    raw = data.get("verify")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'verify' must be a mapping")
    unknown = set(raw) - {"allow_temporary_schema", "cluster"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ConfigError(f"unsupported 'verify' option(s): {names}")
    allow = raw.get("allow_temporary_schema", False)
    if not isinstance(allow, bool):
        raise ConfigError("'verify.allow_temporary_schema' must be a boolean")
    parsed: dict[str, Any] = {"allow_temporary_schema": allow}
    cluster = raw.get("cluster")
    if cluster is None:
        return parsed
    if not isinstance(cluster, dict):
        raise ConfigError("'verify.cluster' must be a mapping")
    cluster_unknown = set(cluster) - {"enabled", "provider", "attestation"}
    if cluster_unknown:
        names = ", ".join(sorted(str(name) for name in cluster_unknown))
        raise ConfigError(f"unsupported 'verify.cluster' option(s): {names}")
    enabled = cluster.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("'verify.cluster.enabled' must be a boolean")

    provider = cluster.get("provider", {})
    if not isinstance(provider, dict):
        raise ConfigError("'verify.cluster.provider' must be a mapping")
    provider_unknown = set(provider) - {
        "adapter",
        "name",
        "sandbox",
        "base_url_env",
        "token_env",
        "timeout",
    }
    if provider_unknown:
        names = ", ".join(sorted(str(name) for name in provider_unknown))
        raise ConfigError(f"unsupported 'verify.cluster.provider' option(s): {names}")

    attestation = cluster.get("attestation", {})
    if not isinstance(attestation, dict):
        raise ConfigError("'verify.cluster.attestation' must be a mapping")
    attestation_unknown = set(attestation) - {"signing_key_env", "key_id"}
    if attestation_unknown:
        names = ", ".join(sorted(str(name) for name in attestation_unknown))
        raise ConfigError(f"unsupported 'verify.cluster.attestation' option(s): {names}")
    parsed["cluster"] = {
        "enabled": enabled,
        "provider": dict(provider),
        "attestation": dict(attestation),
    }
    return parsed


def secret_args_policy_from_mapping(raw: dict[str, Any]) -> SecretArgsPolicy:
    """Build a :class:`SecretArgsPolicy` from a validated mapping."""
    return SecretArgsPolicy(
        enabled=bool(raw.get("enabled", True)),
        policy=str(raw.get("policy", "error")),
        allow_fields=frozenset(str(item) for item in (raw.get("allow_fields") or [])),
        allow_tools=frozenset(str(item) for item in (raw.get("allow_tools") or [])),
        entropy_detection=bool(raw.get("entropy_detection", True)),
    )


def _parse_secret_args(
    data: dict[str, Any],
    *,
    profile: str,
    tools: dict[str, ToolConfig],
) -> dict[str, Any] | None:
    """Optional AF-010 secret-in-args section. Omitted keeps existing behavior."""
    raw = data.get("secret_args")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'secret_args' must be a mapping")
    allowed_keys = {
        "enabled",
        "policy",
        "allow_fields",
        "allow_tools",
        "entropy_detection",
    }
    unknown = set(raw) - allowed_keys
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ConfigError(f"unsupported 'secret_args' option(s): {names}")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("'secret_args.enabled' must be a boolean")
    policy = raw.get("policy", "error")
    if policy not in SECRET_ARGS_POLICIES:
        raise ConfigError(
            f"'secret_args.policy' must be one of {sorted(SECRET_ARGS_POLICIES)}, got {policy!r}"
        )
    allow_fields = raw.get("allow_fields", [])
    if not isinstance(allow_fields, list) or not all(
        isinstance(item, str) and item.strip() for item in allow_fields
    ):
        raise ConfigError(
            "'secret_args.allow_fields' must be a list of non-empty strings; "
            "scope allowlists narrowly by tool, not as a global trust list"
        )
    allow_tools = raw.get("allow_tools", [])
    if not isinstance(allow_tools, list) or not all(
        isinstance(item, str) and item.strip() for item in allow_tools
    ):
        raise ConfigError("'secret_args.allow_tools' must be a list of tool names")
    entropy = raw.get("entropy_detection", True)
    if not isinstance(entropy, bool):
        raise ConfigError("'secret_args.entropy_detection' must be a boolean")

    parsed = {
        "enabled": enabled,
        "policy": str(policy),
        "allow_fields": [str(item).strip() for item in allow_fields],
        "allow_tools": [str(item).strip() for item in allow_tools],
        "entropy_detection": entropy,
    }
    if enabled and profile == PROFILE_PRODUCTION and parsed["policy"] != "error":
        from mycelium.transition import CONSEQUENTIAL_SIDE_EFFECT_CLASSES

        consequential = [
            name
            for name, tool in tools.items()
            if tool.side_effect_class in CONSEQUENTIAL_SIDE_EFFECT_CLASSES
            and name not in parsed["allow_tools"]
        ]
        if consequential:
            _reject_weaker_production_policy("secret_args.policy", parsed["policy"])
    return parsed


def entity_guard_policy_from_mapping(raw: dict[str, Any]) -> EntityGuardPolicy:
    """Build a :class:`EntityGuardPolicy` from a validated mapping."""
    tools: dict[str, ToolDestinationPolicy] = {}
    for name, tool_raw in (raw.get("tools") or {}).items():
        destinations = []
        for spec in tool_raw.get("destinations") or []:
            allow_raw = spec.get("allow") or {}
            destinations.append(
                DestinationSpec(
                    path=str(spec["path"]),
                    dest_type=str(spec["type"]),
                    allow=DestinationAllow(
                        addresses=frozenset(
                            str(item).strip().lower() for item in (allow_raw.get("addresses") or [])
                        ),
                        domains=frozenset(
                            str(item).strip().lower() for item in (allow_raw.get("domains") or [])
                        ),
                        hosts=frozenset(
                            str(item).strip().lower() for item in (allow_raw.get("hosts") or [])
                        ),
                        values=frozenset(
                            str(item).strip() for item in (allow_raw.get("values") or [])
                        ),
                    ),
                    required=bool(spec.get("required", True)),
                    reject_redirects=bool(spec.get("reject_redirects", True)),
                )
            )
        tools[str(name)] = ToolDestinationPolicy(destinations=tuple(destinations))
    return EntityGuardPolicy(
        enabled=bool(raw.get("enabled", True)),
        missing_policy=str(raw.get("missing_policy", MISSING_POLICY_ERROR)),
        policy_version=str(raw.get("policy_version") or "unspecified"),
        tools=tools,
    )


def _parse_string_list(raw: Any, *, field: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) and item.strip() for item in raw):
        raise ConfigError(f"'{field}' must be a list of non-empty strings")
    return [str(item).strip() for item in raw]


def _parse_destination_spec(raw: Any, *, tool: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"entity_guard.tools.{tool}.destinations entries must be mappings")
    allowed_keys = {"path", "type", "allow", "required", "reject_redirects"}
    unknown = set(raw) - allowed_keys
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ConfigError(f"unsupported entity_guard.tools.{tool} destination option(s): {names}")
    path = raw.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ConfigError(f"entity_guard.tools.{tool} destination path is required")
    dest_type = raw.get("type")
    if dest_type not in DEST_TYPES:
        raise ConfigError(
            f"entity_guard.tools.{tool} destination type must be one of "
            f"{sorted(DEST_TYPES)}, got {dest_type!r}"
        )
    allow_raw = raw.get("allow", {})
    if allow_raw is None or allow_raw == []:
        allow_raw = {}
    if not isinstance(allow_raw, dict):
        raise ConfigError(f"entity_guard.tools.{tool} destination allow must be a mapping")
    allow_keys = {"addresses", "domains", "hosts", "values"}
    unknown_allow = set(allow_raw) - allow_keys
    if unknown_allow:
        names = ", ".join(sorted(str(name) for name in unknown_allow))
        raise ConfigError(f"unsupported entity_guard.tools.{tool} allow option(s): {names}")
    required = raw.get("required", True)
    if not isinstance(required, bool):
        raise ConfigError(f"entity_guard.tools.{tool} destination required must be a bool")
    reject_redirects = raw.get("reject_redirects", True)
    if not isinstance(reject_redirects, bool):
        raise ConfigError(f"entity_guard.tools.{tool} destination reject_redirects must be a bool")
    return {
        "path": path.strip(),
        "type": dest_type,
        "allow": {
            "addresses": _parse_string_list(
                allow_raw.get("addresses"),
                field=f"entity_guard.tools.{tool}.allow.addresses",
            ),
            "domains": _parse_string_list(
                allow_raw.get("domains"),
                field=f"entity_guard.tools.{tool}.allow.domains",
            ),
            "hosts": _parse_string_list(
                allow_raw.get("hosts"), field=f"entity_guard.tools.{tool}.allow.hosts"
            ),
            "values": _parse_string_list(
                allow_raw.get("values"), field=f"entity_guard.tools.{tool}.allow.values"
            ),
        },
        "required": required,
        "reject_redirects": reject_redirects,
    }


_AUTHORITY_WINDOW_KEYS = frozenset({"enabled", "use_time_check", "clock_skew_tolerance_seconds"})


def authority_window_policy_from_mapping(raw: dict[str, Any]) -> AuthorityWindowPolicy:
    return AuthorityWindowPolicy(
        enabled=bool(raw.get("enabled", True)),
        use_time_check=str(raw.get("use_time_check", USE_TIME_CHECK_REQUIRED)),
        clock_skew_tolerance_seconds=float(raw.get("clock_skew_tolerance_seconds", 0.0)),
    )


def _parse_authority_window(
    data: dict[str, Any],
    *,
    profile: str,
    destructive_confirm: dict[str, Any] | None,
) -> dict[str, Any] | None:
    raw = data.get("authority_window")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'authority_window' must be a mapping")
    extra = set(raw) - _AUTHORITY_WINDOW_KEYS
    if extra:
        names = ", ".join(sorted(str(item) for item in extra))
        raise ConfigError(f"unsupported 'authority_window' option(s): {names}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("'authority_window.enabled' must be a boolean")
    use_time = raw.get("use_time_check", USE_TIME_CHECK_REQUIRED)
    if use_time not in USE_TIME_CHECKS:
        raise ConfigError(
            f"'authority_window.use_time_check' must be one of {sorted(USE_TIME_CHECKS)}"
        )
    skew = raw.get("clock_skew_tolerance_seconds", 0)
    if (
        not isinstance(skew, (int, float))
        or isinstance(skew, bool)
        or not math.isfinite(skew)
        or skew < 0
    ):
        raise ConfigError(
            "'authority_window.clock_skew_tolerance_seconds' must be a finite number >= 0"
        )
    if profile == PROFILE_PRODUCTION and destructive_confirm is not None:
        if not enabled or use_time != USE_TIME_CHECK_REQUIRED:
            raise ConfigError(
                "profile is 'production' with time-bounded destructive_confirm "
                "but authority_window does not require use-time expiry "
                "(enabled: true, use_time_check: required)"
            )
    return {
        "enabled": enabled,
        "use_time_check": str(use_time),
        "clock_skew_tolerance_seconds": float(skew),
    }


_USE_TIME_TOP_KEYS = frozenset({"enabled", "missing_policy", "policy_version", "tools"})
_USE_TIME_FACT_KEYS = frozenset(
    {
        "name",
        "subject",
        "validator",
        "require",
        "revision_from",
        "max_age_seconds",
        "bind_request_id",
        "bind_run_id",
        "bind_thread_id",
        "compare_to_arg",
        "provider_precondition",
    }
)
_USE_TIME_SUBJECT_KEYS = frozenset({"type", "id_from", "tenant_from", "account_from"})


def use_time_currency_policy_from_mapping(raw: dict[str, Any]) -> UseTimeCurrencyPolicy:
    tools: dict[str, UseTimeToolPolicy] = {}
    for name, tool_raw in (raw.get("tools") or {}).items():
        facts_raw = tool_raw.get("facts") or []
        facts: list[UseTimeFactSpec] = []
        for item in facts_raw:
            subject = item.get("subject") or {}
            require = item.get("require")
            facts.append(
                UseTimeFactSpec(
                    name=str(item["name"]),
                    subject_type=str(subject["type"]),
                    id_from=str(subject["id_from"]),
                    validator=str(item["validator"]),
                    tenant_from=subject.get("tenant_from"),
                    account_from=subject.get("account_from"),
                    require=dict(require) if isinstance(require, dict) else None,
                    revision_from=item.get("revision_from"),
                    max_age_seconds=(
                        float(item["max_age_seconds"])
                        if item.get("max_age_seconds") is not None
                        else None
                    ),
                    bind_request_id=bool(item.get("bind_request_id", False)),
                    bind_run_id=bool(item.get("bind_run_id", False)),
                    bind_thread_id=bool(item.get("bind_thread_id", False)),
                    compare_to_arg=item.get("compare_to_arg"),
                    provider_precondition=item.get("provider_precondition"),
                )
            )
        tools[str(name)] = UseTimeToolPolicy(facts=tuple(facts))
    return UseTimeCurrencyPolicy(
        enabled=bool(raw.get("enabled", True)),
        missing_policy=str(raw.get("missing_policy", USE_TIME_MISSING_POLICY_ERROR)),
        policy_version=str(raw.get("policy_version") or "unspecified"),
        tools=tools,
    )


def _parse_use_time_fact(raw: Any, *, tool: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"use_time_currency.tools.{tool}.facts items must be mappings")
    extra = set(raw) - _USE_TIME_FACT_KEYS
    if extra:
        names = ", ".join(sorted(str(item) for item in extra))
        raise ConfigError(f"unsupported use_time_currency.tools.{tool}.facts option(s): {names}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"use_time_currency.tools.{tool}.facts[].name must be a non-empty string")
    subject = raw.get("subject")
    if not isinstance(subject, dict):
        raise ConfigError(f"use_time_currency.tools.{tool}.facts[].subject must be a mapping")
    subject_extra = set(subject) - _USE_TIME_SUBJECT_KEYS
    if subject_extra:
        names = ", ".join(sorted(str(item) for item in subject_extra))
        raise ConfigError(
            f"unsupported use_time_currency.tools.{tool}.facts[].subject option(s): {names}"
        )
    subject_type = subject.get("type")
    id_from = subject.get("id_from")
    if not isinstance(subject_type, str) or not subject_type.strip():
        raise ConfigError(f"use_time_currency.tools.{tool}.facts[].subject.type is required")
    if not isinstance(id_from, str) or not id_from.strip():
        raise ConfigError(f"use_time_currency.tools.{tool}.facts[].subject.id_from is required")
    validator = raw.get("validator")
    if not isinstance(validator, str) or not validator.strip():
        raise ConfigError(f"use_time_currency.tools.{tool}.facts[].validator is required")
    max_age = raw.get("max_age_seconds")
    if max_age is not None and (
        not isinstance(max_age, (int, float))
        or isinstance(max_age, bool)
        or not math.isfinite(max_age)
        or max_age < 0
    ):
        raise ConfigError(
            f"use_time_currency.tools.{tool}.facts[].max_age_seconds must be a finite number >= 0"
        )
    require = raw.get("require")
    if require is not None and not isinstance(require, dict):
        raise ConfigError(f"use_time_currency.tools.{tool}.facts[].require must be a mapping")
    for key in ("bind_request_id", "bind_run_id", "bind_thread_id"):
        if key in raw and not isinstance(raw[key], bool):
            raise ConfigError(f"use_time_currency.tools.{tool}.facts[].{key} must be a bool")
    for key in ("revision_from", "compare_to_arg", "provider_precondition"):
        if key in raw and raw[key] is not None:
            if not isinstance(raw[key], str) or not str(raw[key]).strip():
                raise ConfigError(
                    f"use_time_currency.tools.{tool}.facts[].{key} must be a non-empty string"
                )
    for key in ("tenant_from", "account_from"):
        if key in subject and subject[key] is not None:
            if not isinstance(subject[key], str) or not str(subject[key]).strip():
                raise ConfigError(
                    f"use_time_currency.tools.{tool}.facts[].subject.{key} must be "
                    "a non-empty string"
                )
    parsed: dict[str, Any] = {
        "name": name.strip(),
        "subject": {
            "type": subject_type.strip(),
            "id_from": id_from.strip(),
        },
        "validator": validator.strip(),
    }
    if subject.get("tenant_from"):
        parsed["subject"]["tenant_from"] = str(subject["tenant_from"]).strip()
    if subject.get("account_from"):
        parsed["subject"]["account_from"] = str(subject["account_from"]).strip()
    if require is not None:
        parsed["require"] = dict(require)
    if raw.get("revision_from"):
        parsed["revision_from"] = str(raw["revision_from"]).strip()
    if max_age is not None:
        parsed["max_age_seconds"] = float(max_age)
    for key in ("bind_request_id", "bind_run_id", "bind_thread_id"):
        if key in raw:
            parsed[key] = bool(raw[key])
    if raw.get("compare_to_arg"):
        parsed["compare_to_arg"] = str(raw["compare_to_arg"]).strip()
    if raw.get("provider_precondition"):
        parsed["provider_precondition"] = str(raw["provider_precondition"]).strip()
    return parsed


def _parse_use_time_currency(
    data: dict[str, Any],
    *,
    profile: str,
    tools: dict[str, ToolConfig],
) -> dict[str, Any] | None:
    raw = data.get("use_time_currency")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'use_time_currency' must be a mapping")
    extra = set(raw) - _USE_TIME_TOP_KEYS
    if extra:
        names = ", ".join(sorted(str(item) for item in extra))
        raise ConfigError(f"unsupported 'use_time_currency' option(s): {names}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("'use_time_currency.enabled' must be a boolean")
    missing_policy = raw.get("missing_policy", USE_TIME_MISSING_POLICY_ERROR)
    if missing_policy not in USE_TIME_MISSING_POLICIES:
        raise ConfigError(
            "'use_time_currency.missing_policy' must be one of "
            f"{sorted(USE_TIME_MISSING_POLICIES)}, got {missing_policy!r}"
        )
    policy_version = raw.get("policy_version")
    if policy_version is not None and (
        not isinstance(policy_version, str) or not policy_version.strip()
    ):
        raise ConfigError("'use_time_currency.policy_version' must be a non-empty string")
    tools_raw = raw.get("tools", {})
    if not isinstance(tools_raw, dict):
        raise ConfigError("'use_time_currency.tools' must be a mapping of tool names")

    parsed_tools: dict[str, Any] = {}
    for name, tool_raw in tools_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("'use_time_currency.tools' keys must be non-empty tool names")
        if not isinstance(tool_raw, dict):
            raise ConfigError(f"use_time_currency.tools.{name} must be a mapping")
        tool_extra = set(tool_raw) - {"facts"}
        if tool_extra:
            names = ", ".join(sorted(str(item) for item in tool_extra))
            raise ConfigError(f"unsupported use_time_currency.tools.{name} option(s): {names}")
        facts_raw = tool_raw.get("facts")
        if not isinstance(facts_raw, list) or not facts_raw:
            raise ConfigError(f"use_time_currency.tools.{name}.facts must be a non-empty list")
        parsed_tools[name.strip()] = {
            "facts": [_parse_use_time_fact(item, tool=name.strip()) for item in facts_raw]
        }

    if enabled and profile == PROFILE_PRODUCTION:
        if missing_policy != USE_TIME_MISSING_POLICY_ERROR:
            _reject_weaker_production_policy(
                "use_time_currency.missing_policy", str(missing_policy)
            )
        for name, tool in tools.items():
            if tool.use_time_currency is False or name not in parsed_tools:
                continue
            # Consequential tools with use_time enabled must declare facts —
            # already enforced by requiring non-empty facts above.

    parsed: dict[str, Any] = {
        "enabled": enabled,
        "missing_policy": str(missing_policy),
        "tools": parsed_tools,
    }
    if policy_version is not None:
        parsed["policy_version"] = str(policy_version).strip()
    return parsed


def _parse_entity_guard(data: dict[str, Any], *, profile: str) -> dict[str, Any] | None:
    """Optional destination-policy section. Omitted keeps existing behavior."""
    raw = data.get("entity_guard")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'entity_guard' must be a mapping")
    allowed_keys = {"enabled", "missing_policy", "policy_version", "tools"}
    unknown = set(raw) - allowed_keys
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ConfigError(f"unsupported 'entity_guard' option(s): {names}")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("'entity_guard.enabled' must be a boolean")
    missing_policy = raw.get("missing_policy", MISSING_POLICY_ERROR)
    if missing_policy not in MISSING_POLICIES:
        raise ConfigError(
            "'entity_guard.missing_policy' must be one of "
            f"{sorted(MISSING_POLICIES)}, got {missing_policy!r}"
        )
    policy_version = raw.get("policy_version")
    if policy_version is not None and (
        not isinstance(policy_version, str) or not policy_version.strip()
    ):
        raise ConfigError("'entity_guard.policy_version' must be a non-empty string")
    tools_raw = raw.get("tools", {})
    if not isinstance(tools_raw, dict):
        raise ConfigError("'entity_guard.tools' must be a mapping of tool names")

    tools: dict[str, Any] = {}
    for name, tool_raw in tools_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("'entity_guard.tools' keys must be non-empty tool names")
        if not isinstance(tool_raw, dict):
            raise ConfigError(f"entity_guard.tools.{name} must be a mapping")
        dests_raw = tool_raw.get("destinations")
        if not isinstance(dests_raw, list) or not dests_raw:
            raise ConfigError(f"entity_guard.tools.{name}.destinations must be a non-empty list")
        extra = set(tool_raw) - {"destinations"}
        if extra:
            names = ", ".join(sorted(str(item) for item in extra))
            raise ConfigError(f"unsupported entity_guard.tools.{name} option(s): {names}")
        tools[name.strip()] = {
            "destinations": [_parse_destination_spec(item, tool=name.strip()) for item in dests_raw]
        }

    if enabled and profile == PROFILE_PRODUCTION and missing_policy != MISSING_POLICY_ERROR:
        _reject_weaker_production_policy("entity_guard.missing_policy", str(missing_policy))

    parsed = {
        "enabled": enabled,
        "missing_policy": str(missing_policy),
        "tools": tools,
    }
    if policy_version is not None:
        parsed["policy_version"] = str(policy_version).strip()
    return parsed


_DESTRUCTIVE_TOP_KEYS = frozenset(
    {
        "enabled",
        "missing_policy",
        "policy_version",
        "storage",
        "path",
        "table",
        "url",
        "url_env",
        "dsn",
        "dsn_env",
        "prefix",
        "tools",
    }
)
_DESTRUCTIVE_TOOL_KEYS = frozenset({"operation", "object", "grant"})
_DESTRUCTIVE_OBJECT_KEYS = frozenset(
    {
        "type",
        "id_from",
        "tenant_from",
        "account_from",
        "case_sensitive",
        "require_canonicalizer",
    }
)
_DESTRUCTIVE_GRANT_KEYS = frozenset(
    {
        "bind_request_id",
        "bind_run_id",
        "bind_thread_id",
        "max_uses",
        "ttl_seconds",
    }
)


def destructive_confirm_policy_from_mapping(raw: dict[str, Any]) -> DestructiveConfirmPolicy:
    tools: dict[str, DestructiveToolPolicy] = {}
    for name, tool_raw in (raw.get("tools") or {}).items():
        object_raw = tool_raw.get("object") or {}
        grant_raw = tool_raw.get("grant") or {}
        tools[str(name)] = DestructiveToolPolicy(
            operation=str(tool_raw["operation"]),
            object=DestructiveObjectSpec(
                object_type=str(object_raw["type"]),
                id_from=str(object_raw["id_from"]),
                tenant_from=object_raw.get("tenant_from"),
                account_from=object_raw.get("account_from"),
                case_sensitive=bool(object_raw.get("case_sensitive", True)),
                require_canonicalizer=bool(object_raw.get("require_canonicalizer", False)),
            ),
            grant=DestructiveGrantSpec(
                bind_request_id=bool(grant_raw.get("bind_request_id", False)),
                bind_run_id=bool(grant_raw.get("bind_run_id", False)),
                bind_thread_id=bool(grant_raw.get("bind_thread_id", False)),
                max_uses=int(grant_raw.get("max_uses", 1)),
                ttl_seconds=float(grant_raw.get("ttl_seconds", 300)),
            ),
        )
    return DestructiveConfirmPolicy(
        enabled=bool(raw.get("enabled", True)),
        missing_policy=str(raw.get("missing_policy", DESTRUCTIVE_MISSING_POLICY_ERROR)),
        policy_version=str(raw.get("policy_version") or "unspecified"),
        storage=str(raw.get("storage") or STORAGE_MEMORY),
        tools=tools,
    )


def _parse_destructive_object(raw: Any, *, tool: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ConfigError(f"destructive_confirm.tools.{tool}.object must be a mapping")
    extra = set(raw) - _DESTRUCTIVE_OBJECT_KEYS
    if extra:
        names = ", ".join(sorted(str(item) for item in extra))
        raise ConfigError(f"unsupported destructive_confirm.tools.{tool}.object option(s): {names}")
    object_type = raw.get("type")
    id_from = raw.get("id_from")
    if not isinstance(object_type, str) or not object_type.strip():
        raise ConfigError(f"destructive_confirm.tools.{tool}.object.type is required")
    if not isinstance(id_from, str) or not id_from.strip():
        raise ConfigError(f"destructive_confirm.tools.{tool}.object.id_from is required")
    parsed: dict[str, Any] = {
        "type": object_type.strip(),
        "id_from": id_from.strip(),
        "case_sensitive": True,
        "require_canonicalizer": False,
    }
    if "case_sensitive" in raw:
        if not isinstance(raw["case_sensitive"], bool):
            raise ConfigError(
                f"destructive_confirm.tools.{tool}.object.case_sensitive must be a bool"
            )
        parsed["case_sensitive"] = raw["case_sensitive"]
    if "require_canonicalizer" in raw:
        if not isinstance(raw["require_canonicalizer"], bool):
            raise ConfigError(
                f"destructive_confirm.tools.{tool}.object.require_canonicalizer must be a bool"
            )
        parsed["require_canonicalizer"] = raw["require_canonicalizer"]
    for key in ("tenant_from", "account_from"):
        if key not in raw:
            continue
        value = raw[key]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(
                f"destructive_confirm.tools.{tool}.object.{key} must be a non-empty string"
            )
        parsed[key] = value.strip()
    return parsed


def _parse_destructive_grant(raw: Any, *, tool: str) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"destructive_confirm.tools.{tool}.grant must be a mapping")
    extra = set(raw) - _DESTRUCTIVE_GRANT_KEYS
    if extra:
        names = ", ".join(sorted(str(item) for item in extra))
        raise ConfigError(f"unsupported destructive_confirm.tools.{tool}.grant option(s): {names}")
    parsed: dict[str, Any] = {
        "bind_request_id": False,
        "bind_run_id": False,
        "bind_thread_id": False,
        "max_uses": 1,
        "ttl_seconds": 300.0,
    }
    for key in ("bind_request_id", "bind_run_id", "bind_thread_id"):
        if key not in raw:
            continue
        if not isinstance(raw[key], bool):
            raise ConfigError(f"destructive_confirm.tools.{tool}.grant.{key} must be a bool")
        parsed[key] = raw[key]
    if "max_uses" in raw:
        value = raw["max_uses"]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(
                f"destructive_confirm.tools.{tool}.grant.max_uses must be an integer >= 1"
            )
        parsed["max_uses"] = value
    if "ttl_seconds" in raw:
        value = raw["ttl_seconds"]
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ConfigError(f"destructive_confirm.tools.{tool}.grant.ttl_seconds must be > 0")
        parsed["ttl_seconds"] = float(value)
    return parsed


def _parse_destructive_confirm(
    data: dict[str, Any],
    *,
    profile: str,
    tools: dict[str, ToolConfig],
    deployment: dict[str, Any] | None,
) -> dict[str, Any] | None:
    raw = data.get("destructive_confirm")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'destructive_confirm' must be a mapping")
    extra = set(raw) - _DESTRUCTIVE_TOP_KEYS
    if extra:
        names = ", ".join(sorted(str(item) for item in extra))
        raise ConfigError(f"unsupported 'destructive_confirm' option(s): {names}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("'destructive_confirm.enabled' must be a boolean")
    missing_policy = raw.get("missing_policy", DESTRUCTIVE_MISSING_POLICY_ERROR)
    if missing_policy not in DESTRUCTIVE_MISSING_POLICIES:
        raise ConfigError(
            "'destructive_confirm.missing_policy' must be one of "
            f"{sorted(DESTRUCTIVE_MISSING_POLICIES)}"
        )
    policy_version = raw.get("policy_version")
    if policy_version is not None and (
        not isinstance(policy_version, str) or not policy_version.strip()
    ):
        raise ConfigError("'destructive_confirm.policy_version' must be a non-empty string")
    storage = raw.get("storage", STORAGE_MEMORY)
    if storage not in {
        STORAGE_MEMORY,
        STORAGE_FILE,
        STORAGE_SQLITE,
        STORAGE_REDIS,
        STORAGE_POSTGRES,
    }:
        raise ConfigError(
            "'destructive_confirm.storage' must be one of memory, file, sqlite, redis, postgres"
        )
    tools_raw = raw.get("tools", {})
    if not isinstance(tools_raw, dict):
        raise ConfigError("'destructive_confirm.tools' must be a mapping of tool names")
    parsed_tools: dict[str, Any] = {}
    for name, tool_raw in tools_raw.items():
        if not isinstance(name, str) or not name.strip():
            raise ConfigError("'destructive_confirm.tools' keys must be non-empty tool names")
        if not isinstance(tool_raw, dict):
            raise ConfigError(f"destructive_confirm.tools.{name} must be a mapping")
        extra_tool = set(tool_raw) - _DESTRUCTIVE_TOOL_KEYS
        if extra_tool:
            names = ", ".join(sorted(str(item) for item in extra_tool))
            raise ConfigError(f"unsupported destructive_confirm.tools.{name} option(s): {names}")
        operation = tool_raw.get("operation")
        if not isinstance(operation, str) or not operation.strip():
            raise ConfigError(f"destructive_confirm.tools.{name}.operation is required")
        object_spec = _parse_destructive_object(tool_raw.get("object"), tool=name.strip())
        grant_spec = _parse_destructive_grant(tool_raw.get("grant"), tool=name.strip())
        parsed_tools[name.strip()] = {
            "operation": operation.strip(),
            "object": object_spec,
            "grant": grant_spec,
        }

    if enabled and profile == PROFILE_PRODUCTION:
        if missing_policy != DESTRUCTIVE_MISSING_POLICY_ERROR:
            _reject_weaker_production_policy(
                "destructive_confirm.missing_policy", str(missing_policy)
            )
        if storage == STORAGE_MEMORY:
            raise ConfigError(
                "profile is 'production' but destructive_confirm.storage is "
                "'memory'; production requires durable grant storage "
                "(file, sqlite, redis, or postgres)"
            )
        topology = (deployment or {}).get("topology")
        if topology == "multi_node" and storage not in SHARED_GRANT_STORAGES:
            raise ConfigError(
                "profile is 'production' and deployment.topology is 'multi_node' "
                "but destructive_confirm.storage is "
                f"{storage!r}; multi-node production requires redis or postgres"
            )
        for name, tool in tools.items():
            if tool.side_effect_class != SideEffectClass.IRREVERSIBLE:
                continue
            if tool.destructive_confirm is False or name not in parsed_tools:
                raise ConfigError(
                    f"profile is 'production' and tool {name!r} is "
                    "side_effect_class: irreversible but has no "
                    "destructive_confirm.tools declaration. Do not infer "
                    "destructiveness from the tool name; list the tool with "
                    "operation, object type, and id_from."
                )

    parsed: dict[str, Any] = {
        "enabled": enabled,
        "missing_policy": str(missing_policy),
        "storage": storage,
        "tools": parsed_tools,
    }
    if policy_version is not None:
        parsed["policy_version"] = str(policy_version).strip()
    for key in ("path", "table", "url", "url_env", "dsn", "dsn_env", "prefix"):
        if key in raw:
            parsed[key] = raw[key]
    return parsed


def _parse_config(
    data: dict[str, Any],
    *,
    activate_runtime: bool = True,
) -> MyceliumConfig:
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")

    config_version = data.get("config_version", CONFIG_VERSION)
    if config_version != CONFIG_VERSION:
        raise ConfigError(
            f"unsupported config_version {config_version!r}; this Mycelium "
            f"runtime supports version {CONFIG_VERSION}. Upgrade Mycelium or "
            "migrate the file after reviewing the release notes"
        )

    profile = _parse_profile(data)

    state_backend_raw = data.get("state_backend")
    if state_backend_raw is not None and not isinstance(state_backend_raw, dict):
        raise ConfigError("'state_backend' must be a mapping")
    if state_backend_raw is not None:
        storage_type = state_backend_raw.get("storage", "memory")
        if storage_type not in ("memory", "file", "redis", "postgres"):
            raise ConfigError(f"unknown state_backend storage type: {storage_type!r}")
        if storage_type == "file" and not state_backend_raw.get("path"):
            raise ConfigError("state_backend storage 'file' requires a 'path'")

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
            "'audit_receipt.agent_id' is no longer supported; set 'transition.agent_id' instead"
        )
    if audit_receipt_raw is not None:
        storage_type = audit_receipt_raw.get("storage")
        if storage_type not in (None, "memory", "file", "redis", "postgres", "shared"):
            raise ConfigError(f"unknown audit_receipt storage type: {storage_type!r}")
        if storage_type == "file" and not audit_receipt_raw.get("path"):
            raise ConfigError("audit_receipt storage 'file' requires a 'path'")
        if storage_type == "shared" and state_backend_raw is None:
            raise ConfigError("audit_receipt storage 'shared' requires state_backend")

    audit_auto = False
    if audit_receipt_raw is not None:
        audit_auto = _parse_bool_option(
            audit_receipt_raw,
            "auto",
            field="'audit_receipt.auto'",
            default=bool(audit_receipt_raw),
        )

    outcome_emit_raw = data.get("outcome_emit")
    if outcome_emit_raw is not None and not isinstance(outcome_emit_raw, dict):
        raise ConfigError("'outcome_emit' must be a mapping")
    if outcome_emit_raw is not None and outcome_emit_raw.get("agent_id"):
        raise ConfigError(
            "'outcome_emit.agent_id' is no longer supported; set 'transition.agent_id' instead"
        )

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
    _enforce_memory_storage_policy(tools, transition, action_ledger_raw, profile=profile)
    _request_identity_policy(action_ledger_raw, profile=profile)
    _enforce_production_outcome_emit(outcome_emit_raw, profile=profile)

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

    _validate_callable_targets(tools, tasks)

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

    loop_guard_raw = data.get("loop_guard")
    if loop_guard_raw is not None and not isinstance(loop_guard_raw, dict):
        raise ConfigError("'loop_guard' must be a mapping")
    if loop_guard_raw is not None:
        # Validate early so half-wired configs fail at load.
        storage_type = loop_guard_raw.get("storage", "memory")
        if storage_type == "file" and not loop_guard_raw.get("path"):
            raise ConfigError("loop_guard storage 'file' requires a 'path'")
        if storage_type not in ("memory", "file", "redis", "postgres", "shared"):
            raise ConfigError(f"unknown loop_guard storage type: {storage_type!r}")
        if storage_type == "shared" and state_backend_raw is None:
            raise ConfigError("loop_guard storage 'shared' requires state_backend")
        tools_sel = loop_guard_raw.get("tools", "all")
        if tools_sel != "all" and not isinstance(tools_sel, list):
            raise ConfigError("'loop_guard.tools' must be 'all' or a list of tool names")
        _missing_run_id_policy(
            loop_guard_raw,
            "loop_guard.missing_run_id_policy",
            profile=profile,
        )

    budget_raw = data.get("budget")
    if budget_raw is not None and not isinstance(budget_raw, dict):
        raise ConfigError("'budget' must be a mapping")
    if budget_raw is not None:
        storage_type = budget_raw.get("storage", "memory")
        if storage_type == "file" and not budget_raw.get("path"):
            raise ConfigError("budget storage 'file' requires a 'path'")
        if storage_type == "sqlite" and not budget_raw.get("path"):
            raise ConfigError("budget storage 'sqlite' requires a 'path'")
        if storage_type not in (
            "memory",
            "file",
            "sqlite",
            "redis",
            "postgres",
        ):
            raise ConfigError(f"unknown budget storage type: {storage_type!r}")
        tools_sel = budget_raw.get("tools", "all")
        if tools_sel != "all" and not isinstance(tools_sel, list):
            raise ConfigError("'budget.tools' must be 'all' or a list of tool names")
        _budget_ceilings_from_config(budget_raw)
        warn_at = budget_raw.get("warn_at", 0.8)
        try:
            warn_at_f = float(warn_at)
        except (TypeError, ValueError) as exc:
            raise ConfigError("'budget.warn_at' must be a float in (0, 1]") from exc
        if not 0.0 < warn_at_f <= 1.0:
            raise ConfigError("'budget.warn_at' must be a float in (0, 1]")
        on_missing = budget_raw.get("on_missing_meter", ON_MISSING_HARD)
        if on_missing not in ON_MISSING_METER_MODES:
            raise ConfigError(
                f"'budget.on_missing_meter' must be one of {sorted(ON_MISSING_METER_MODES)}"
            )
        _missing_usage_policy(budget_raw, profile=profile)

    scope_guard_raw = data.get("scope_guard")
    if scope_guard_raw is not None and not isinstance(scope_guard_raw, dict):
        raise ConfigError("'scope_guard' must be a mapping")
    if scope_guard_raw is not None:
        storage_type = scope_guard_raw.get("storage", "memory")
        if storage_type == "file" and not scope_guard_raw.get("path"):
            raise ConfigError("scope_guard storage 'file' requires a 'path'")
        if storage_type not in ("memory", "file", "redis", "postgres", "shared"):
            raise ConfigError(f"unknown scope_guard storage type: {storage_type!r}")
        if storage_type == "shared" and state_backend_raw is None:
            raise ConfigError("scope_guard storage 'shared' requires state_backend")
        tools_sel = scope_guard_raw.get("tools", "all")
        if tools_sel != "all" and not isinstance(tools_sel, list):
            raise ConfigError("'scope_guard.tools' must be 'all' or a list of tool names")
        on_violation = scope_guard_raw.get("on_violation", ON_VIOLATION_SOFT)
        if on_violation not in ON_VIOLATION_MODES:
            raise ConfigError(
                f"'scope_guard.on_violation' must be one of {sorted(ON_VIOLATION_MODES)}"
            )
        _missing_run_id_policy(
            scope_guard_raw,
            "scope_guard.missing_run_id_policy",
            profile=profile,
        )
        _scope_grant_from_config(
            scope_guard_raw,
            registry_allowed=registry_allowed,
            tool_names=list(tools.keys()),
        )

    completion_raw = data.get("completion")
    if completion_raw is not None and not isinstance(completion_raw, dict):
        raise ConfigError("'completion' must be a mapping")
    if completion_raw is not None:
        storage_type = completion_raw.get("storage", "memory")
        if storage_type == "file" and not completion_raw.get("path"):
            raise ConfigError("completion storage 'file' requires a 'path'")
        if storage_type not in ("memory", "file", "redis", "postgres", "shared"):
            raise ConfigError(f"unknown completion storage type: {storage_type!r}")
        if storage_type == "shared" and state_backend_raw is None:
            raise ConfigError("completion storage 'shared' requires state_backend")
        installer_path = completion_raw.get("adapter_installer")
        if installer_path is not None and (
            not isinstance(installer_path, str) or not _CALLABLE_PATH_RE.fullmatch(installer_path)
        ):
            raise ConfigError("'completion.adapter_installer' must be 'package.module:function'")
        _parse_completion_id_lists(completion_raw)

    state_authority_raw = data.get("state_authority")
    if state_authority_raw is not None and not isinstance(state_authority_raw, dict):
        raise ConfigError("'state_authority' must be a mapping")
    if state_authority_raw is not None:
        callable_path = state_authority_raw.get("canonical_callable")
        if not isinstance(callable_path, str) or not callable_path:
            raise ConfigError(
                "'state_authority.canonical_callable' is required "
                "(format: 'package.module:function')"
            )
        _parse_callable_path(callable_path, kind="state_authority", name="canonical_callable")
        tools_sel = state_authority_raw.get("tools", "all")
        if tools_sel != "all" and not isinstance(tools_sel, list):
            raise ConfigError("'state_authority.tools' must be 'all' or a list of tool names")
        on_mismatch = state_authority_raw.get("on_mismatch", ON_MISMATCH_HARD)
        if on_mismatch not in ON_MISMATCH_MODES:
            raise ConfigError(
                f"'state_authority.on_mismatch' must be one of {sorted(ON_MISMATCH_MODES)}"
            )
        on_missing = state_authority_raw.get("on_missing", ON_MISMATCH_HARD)
        if on_missing not in ON_MISMATCH_MODES:
            raise ConfigError(
                f"'state_authority.on_missing' must be one of {sorted(ON_MISMATCH_MODES)}"
            )
        if "require_state_ref" in state_authority_raw and not isinstance(
            state_authority_raw.get("require_state_ref"), bool
        ):
            raise ConfigError("'state_authority.require_state_ref' must be a bool")
        exclude = state_authority_raw.get("exclude") or []
        if not isinstance(exclude, list):
            raise ConfigError("'state_authority.exclude' must be a list of tool names")

    secret_args_raw = _parse_secret_args(data, profile=profile, tools=tools)
    entity_guard_raw = _parse_entity_guard(data, profile=profile)
    # destructive_confirm is parsed after deployment so topology can be checked.

    message_validator_raw = data.get("message_validator", False)
    if isinstance(message_validator_raw, dict):
        message_validator = bool(message_validator_raw.get("enabled", True))
    else:
        message_validator = bool(message_validator_raw)

    state_flush_raw = data.get("state_flush")
    if state_flush_raw is not None and not isinstance(state_flush_raw, dict):
        raise ConfigError("'state_flush' must be a mapping")
    if state_flush_raw is not None:
        storage_type = state_flush_raw.get("storage")
        if storage_type not in (None, "memory", "file", "redis", "postgres", "shared"):
            raise ConfigError(f"unknown state_flush storage type: {storage_type!r}")
        if storage_type == "file" and not state_flush_raw.get("path"):
            raise ConfigError("state_flush storage 'file' requires a 'path'")
        if storage_type == "shared" and state_backend_raw is None:
            raise ConfigError("state_flush storage 'shared' requires state_backend")

    integrations = _parse_integrations(data)
    deployment = _parse_deployment(data)
    verify = _parse_verify(data)
    destructive_confirm_raw = _parse_destructive_confirm(
        data, profile=profile, tools=tools, deployment=deployment
    )
    authority_window_raw = _parse_authority_window(
        data, profile=profile, destructive_confirm=destructive_confirm_raw
    )
    use_time_currency_raw = _parse_use_time_currency(data, profile=profile, tools=tools)

    cfg = MyceliumConfig(
        tools=tools,
        tasks=tasks,
        registry_allowed=registry_allowed,
        runner_settings=runner_raw,
        config_version=CONFIG_VERSION,
        history_guard=history_guard_raw,
        message_validator=message_validator,
        state_flush=state_flush_raw,
        audit_receipt=audit_receipt_raw,
        state_backend=state_backend_raw,
        outcome_emit=outcome_emit_raw,
        transition=transition,
        action_ledger=action_ledger_raw,
        task_ledger_defaults=task_ledger_raw,
        integrations=integrations,
        loop_guard=loop_guard_raw,
        budget=budget_raw,
        scope_guard=scope_guard_raw,
        state_authority=state_authority_raw,
        completion=completion_raw,
        deployment=deployment,
        verify=verify,
        secret_args=secret_args_raw,
        entity_guard=entity_guard_raw,
        destructive_confirm=destructive_confirm_raw,
        authority_window=authority_window_raw,
        use_time_currency=use_time_currency_raw,
        profile=profile,
        _audit_auto=audit_auto,
    )
    if activate_runtime:
        cfg._activate_completion_terminal()
        cfg._activate_llm_budget()
        if authority_window_raw is not None or destructive_confirm_raw is not None:
            cfg._activate_authority_window()
        if use_time_currency_raw is not None:
            cfg._activate_use_time_currency()
    return cfg


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


def _load_config_for_preflight(path: str | Path) -> MyceliumConfig:
    """Validate config without activating application-owned runtime hooks."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML: {exc}") from exc
    return _parse_config(data or {}, activate_runtime=False)


__all__ = [
    "ConfigError",
    "MEMORY_STORAGE_POLICIES",
    "MEMORY_STORAGE_POLICY_ERROR",
    "MEMORY_STORAGE_POLICY_WARN",
    "PROFILE_DEVELOPMENT",
    "PROFILE_PRODUCTION",
    "PROFILES",
    "REQUEST_IDENTITY_POLICIES",
    "REQUEST_IDENTITY_POLICY_DERIVED",
    "REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT",
    "MyceliumConfig",
    "ToolConfig",
    "TransitionConfig",
    "config_json_schema",
    "load_config",
    "load_config_from_string",
]
