"""YAML parsing, normalization, and production-profile validation."""

from __future__ import annotations

import math
import warnings
from typing import TYPE_CHECKING, Any

from mycelium.authority_window import (
    USE_TIME_CHECK_REQUIRED,
    USE_TIME_CHECKS,
    AuthorityWindowPolicy,
)
from mycelium.budget_guard import (
    ON_MISSING_HARD,
    ON_MISSING_METER_MODES,
)
from mycelium.config_policies import (
    _CALLABLE_PATH_RE,
    _budget_ceilings_from_config,
    _merge_storage_settings,
    _missing_usage_policy,
    _parse_callable_path,
    _reject_weaker_production_policy,
    _storage_settings,
)
from mycelium.config_schema import (
    CONFIG_VERSION,
    ToolContractModel,
)
from mycelium.config_types import (
    MEMORY_STORAGE_POLICIES,
    MEMORY_STORAGE_POLICY_ERROR,
    MEMORY_STORAGE_POLICY_WARN,
    PROFILE_DEVELOPMENT,
    PROFILE_PRODUCTION,
    PROFILES,
    ConfigError,
    TaskConfig,
    ToolConfig,
)
from mycelium.contracts import validate_contract_definition
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
)
from mycelium.entity_guard import (
    DEST_TYPES,
    MISSING_POLICIES,
    MISSING_POLICY_ERROR,
    DestinationAllow,
    DestinationSpec,
    EntityGuardPolicy,
    ToolDestinationPolicy,
)
from mycelium.loop_guard import (
    MISSING_RUN_ID_POLICIES,
    MISSING_RUN_ID_POLICY_ERROR,
    MISSING_RUN_ID_POLICY_WARN,
)
from mycelium.outcome_emit import (
    OUTCOME_ON_FAILURE_ERROR,
    OUTCOME_ON_FAILURE_POLICIES,
    OUTCOME_ON_FAILURE_WARN,
)
from mycelium.scope_guard import (
    ON_VIOLATION_MODES,
    ON_VIOLATION_SOFT,
    ScopeGrant,
)
from mycelium.secret_protection import (
    SECRET_ARGS_POLICIES,
    SecretArgsPolicy,
)
from mycelium.state_authority import (
    ON_MISMATCH_HARD,
    ON_MISMATCH_MODES,
)
from mycelium.transition import (
    REQUEST_IDENTITY_POLICIES,
    REQUEST_IDENTITY_POLICY_DERIVED,
    REQUEST_IDENTITY_POLICY_REQUIRE_EXPLICIT,
    RetryPermission,
    SideEffectBoundary,
    SideEffectClass,
    Spendability,
    ToolCapability,
    TransitionConfig,
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
)

if TYPE_CHECKING:
    from mycelium.runtime_builder import MyceliumConfig

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
            capability=existing.capability,
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
            contract=existing.contract,
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
    if isinstance(value, bool):
        raise ConfigError(f"'{section}.{key}' must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{section}.{key}' must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
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
    if isinstance(value, bool):
        raise ConfigError(f"'{section}.{key}' must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"'{section}.{key}' must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise ConfigError(f"'{section}.{key}' must be greater than or equal to zero")
    return parsed


def _parse_transition_config(raw: Any) -> TransitionConfig | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ConfigError("'transition' must be a mapping")

    agent_id = raw.get("agent_id")
    policy_version = raw.get("policy_version")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise ConfigError("'transition.agent_id' must be a non-empty string")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ConfigError("'transition.policy_version' must be a non-empty string")

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

    reclaim_requires_death_signal = raw.get("reclaim_requires_death_signal", True)
    if not isinstance(reclaim_requires_death_signal, bool):
        raise ConfigError("'transition.reclaim_requires_death_signal' must be a boolean")
    presumed_dead_after = _parse_optional_positive_float(
        raw, "presumed_dead_after", section="transition"
    )

    return TransitionConfig(
        agent_id=agent_id.strip(),
        policy_version=policy_version.strip(),
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

    unknown = set(raw) - {"langgraph", "crewai"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ConfigError(f"unsupported integration(s): {names}")

    parsed: dict[str, dict[str, Any]] = {}
    langgraph_raw = raw.get("langgraph")
    if langgraph_raw is not None:
        if isinstance(langgraph_raw, bool):
            parsed["langgraph"] = {"enabled": langgraph_raw}
        else:
            if not isinstance(langgraph_raw, dict):
                raise ConfigError(
                    "'integrations.langgraph' must be a mapping or boolean"
                )
            unknown_langgraph = set(langgraph_raw) - {"enabled"}
            if unknown_langgraph:
                names = ", ".join(sorted(str(name) for name in unknown_langgraph))
                raise ConfigError(
                    f"unsupported 'integrations.langgraph' option(s): {names}"
                )
            enabled = langgraph_raw.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ConfigError("'integrations.langgraph.enabled' must be a boolean")
            parsed["langgraph"] = {"enabled": enabled}

    crewai_raw = raw.get("crewai")
    if crewai_raw is not None:
        if isinstance(crewai_raw, bool):
            parsed["crewai"] = {"enabled": crewai_raw}
        else:
            if not isinstance(crewai_raw, dict):
                raise ConfigError("'integrations.crewai' must be a mapping or boolean")
            unknown_crewai = set(crewai_raw) - {"enabled", "run_id_from"}
            if unknown_crewai:
                names = ", ".join(sorted(str(name) for name in unknown_crewai))
                raise ConfigError(
                    f"unsupported 'integrations.crewai' option(s): {names}"
                )
            enabled = crewai_raw.get("enabled", True)
            if not isinstance(enabled, bool):
                raise ConfigError("'integrations.crewai.enabled' must be a boolean")
            run_id_from = crewai_raw.get("run_id_from")
            if run_id_from is not None and (
                not isinstance(run_id_from, str) or not run_id_from.strip()
            ):
                raise ConfigError(
                    "'integrations.crewai.run_id_from' must be a non-empty string"
                )
            parsed["crewai"] = {
                "enabled": enabled,
                **(
                    {"run_id_from": run_id_from.strip()}
                    if isinstance(run_id_from, str)
                    else {}
                ),
            }
    return parsed


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
            f"use_time_currency.tools.{tool}.facts[].max_age_seconds "
            "must be a finite number >= 0"
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
    from mycelium.runtime_builder import MyceliumConfig

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
    crewai_integration = (integrations or {}).get("crewai", {})
    if (
        profile == PROFILE_PRODUCTION
        and crewai_integration.get("enabled", False)
        and not crewai_integration.get("run_id_from")
    ):
        raise ConfigError(
            "profile is 'production' and integrations.crewai is enabled, but "
            "'integrations.crewai.run_id_from' is missing. Bind it to a stable, "
            "host-owned Crew.kickoff input field"
        )
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
        cfg._activate_framework_integrations()
        cfg._activate_completion_terminal()
        cfg._activate_llm_budget()
        if authority_window_raw is not None or destructive_confirm_raw is not None:
            cfg._activate_authority_window()
        if use_time_currency_raw is not None:
            cfg._activate_use_time_currency()
    return cfg
