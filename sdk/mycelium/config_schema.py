"""Versioned, machine-readable model for ``mycelium.yaml``.

This module owns the structural configuration contract used by editors and
agents.  The semantic parser in :mod:`mycelium.config` remains authoritative
for cross-field safety rules and runtime object construction.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

CONFIG_VERSION = 1
CONFIG_SCHEMA_ID = "https://mycelium-labs.github.io/schema/mycelium-config-v1.json"


class _ConfigModel(BaseModel):
    """Strictly type declared fields while preserving extension compatibility."""

    model_config = ConfigDict(extra="allow", strict=True)


class StorageConfigModel(_ConfigModel):
    """Common durable-state backend settings."""

    storage: str | None = Field(
        default=None,
        json_schema_extra={
            "enum": ["memory", "file", "sqlite", "redis", "postgres", "shared", None]
        },
    )
    path: str | None = None
    table: str | None = None
    namespace: str | None = None
    prefix: str | None = None
    url: str | None = None
    url_env: str | None = None
    dsn: str | None = None
    dsn_env: str | None = None


class BudgetConfigModel(StorageConfigModel):
    """Run-wide ceilings. Omit this section to disable; enabled is unsupported."""

    model_config = ConfigDict(extra="forbid", strict=True)
    storage: Literal["memory", "file", "sqlite", "redis", "postgres"] = "memory"
    tools: Literal["all"] | list[str] = "all"
    exclude: list[str] = Field(default_factory=list)

    max_duration: float | str | None = Field(
        default=None,
        description="Wall-clock ceiling for the run, in seconds or with a duration suffix.",
    )
    max_steps: int | None = Field(
        default=None,
        gt=0,
        description=(
            "Run-wide protected-call ceiling. Each budget-guarded tool invocation and "
            "instrumented LLM turn reserves one step; business workflow counters are separate."
        ),
    )
    max_tokens: int | None = Field(default=None, gt=0)
    max_usd: int | float | None = Field(default=None, gt=0)
    max_cost_usd: int | float | None = Field(default=None, gt=0)
    missing_usage_policy: Literal["warn", "error"] | None = None
    warn_at: int | float | None = Field(default=None, gt=0, le=1)
    on_missing_meter: Literal["warn", "hard"] | None = None


class CompletionConfigModel(StorageConfigModel):
    """Host checklist. Omit this section to disable; enabled is unsupported."""

    model_config = ConfigDict(extra="forbid", strict=True)
    storage: Literal["memory", "file", "redis", "postgres", "shared"] | None = Field(
        default=None, description="Omitted storage inherits state_backend, otherwise uses memory."
    )
    required: list[str | dict[str, Any]] = Field(default_factory=list)
    optional: list[str | dict[str, Any]] = Field(default_factory=list)

    adapter_installer: str | None = Field(
        default=None,
        description=(
            "Import path (package.module:function) called during runtime config "
            "activation. It must wire the custom terminal boundary and call "
            "register_terminal_adapter()."
        ),
    )


class LoopGuardConfigModel(StorageConfigModel):
    """Consecutive-action limits. Omit this section to disable; enabled is unsupported."""

    model_config = ConfigDict(extra="forbid", strict=True)
    storage: Literal["memory", "file", "redis", "postgres", "shared"] | None = Field(
        default=None, description="Omitted storage inherits state_backend, otherwise uses memory."
    )
    tools: Literal["all"] | list[str] = "all"
    exclude: list[str] = Field(default_factory=list)
    consecutive_soft: dict[str, int] | None = None
    escalate_after_soft: int = Field(default=1, gt=0)
    unclassified_policy: Literal["warn", "strict"] = "warn"
    missing_run_id_policy: Literal["warn", "error"] | None = None


class ScopeGuardConfigModel(StorageConfigModel):
    """Frozen tool scope. Omit this section to disable; enabled is unsupported."""

    model_config = ConfigDict(extra="forbid", strict=True)
    storage: Literal["memory", "file", "redis", "postgres", "shared"] | None = Field(
        default=None, description="Omitted storage inherits state_backend, otherwise uses memory."
    )
    tools: Literal["all"] | list[str] = "all"
    exclude: list[str] = Field(default_factory=list)
    allowed_tools: Literal["from_registry", "all"] | list[str] = "from_registry"
    on_violation: Literal["soft", "hard"] = "soft"
    auto_bind: bool = True
    missing_run_id_policy: Literal["warn", "error"] | None = None


class StateAuthorityConfigModel(_ConfigModel):
    """Compare the host's frozen state reference with its current canonical reference."""

    canonical_callable: str = Field(description="Host resolver path: package.module:function.")
    require_state_ref: bool = False
    on_mismatch: Literal["soft", "hard"] = "hard"
    on_missing: Literal["soft", "hard"] = "hard"
    tools: Literal["all"] | list[str] = "all"
    exclude: list[str] = Field(default_factory=list)


class TransitionConfigModel(_ConfigModel):
    """Stable identity and retry timing for guarded transitions."""

    agent_id: str
    policy_version: str
    scope_from: dict[str, str] = Field(default_factory=dict)
    lease_ttl: float | None = Field(default=None, gt=0)
    lease_renew_interval: float | None = Field(default=None, ge=0)
    poll_interval: float | None = Field(default=None, gt=0)
    poll_timeout: float | None = Field(default=None, gt=0)
    reclaim_requires_death_signal: bool = True
    presumed_dead_after: float | None = Field(default=None, gt=0)


class LedgerPayloadPolicyModel(_ConfigModel):
    store_args: bool = True
    store_result: bool = True
    redact_fields: list[str] = Field(default_factory=list)


class LedgerConfigModel(StorageConfigModel):
    """Defaults and allowlist for tool-level durable execution."""

    tools: Literal["all"] | list[str] | None = None
    unclassified_policy: str | None = Field(
        default=None, json_schema_extra={"enum": ["warn", "strict", None]}
    )
    memory_storage_policy: str | None = Field(
        default=None, json_schema_extra={"enum": ["warn", "error", None]}
    )
    request_identity_policy: str | None = Field(
        default=None,
        json_schema_extra={"enum": ["derived", "require_explicit", None]},
    )
    # Runtime validation intentionally remains in ActionLedger construction so
    # existing applications keep the same load-versus-use failure timing.
    on_args_drift: str | None = Field(
        default=None,
        json_schema_extra={"enum": ["soft", "hard", "off"]},
    )
    missing_run_id_policy: str | None = Field(
        default=None, json_schema_extra={"enum": ["warn", "error", None]}
    )
    payload_policy: LedgerPayloadPolicyModel | None = None


class TaskLedgerConfigModel(StorageConfigModel):
    """Defaults and allowlist for task-level durable execution."""

    tasks: Literal["all"] | list[str] | None = None


class ToolContractModel(_ConfigModel):
    """Optional deterministic developer contract for a tool.

    Type names are the small v1 subset: string, integer, number, boolean,
    array, object, and null. Schema values may be a type name or a mapping
    with ``type`` (and for arrays, ``items``).
    """

    operations: list[str] = Field(default_factory=list)
    required_args: list[str] = Field(default_factory=list)
    optional_args: list[str] = Field(default_factory=list)
    argument_types: dict[str, str | dict[str, Any]] = Field(default_factory=dict)
    output_schema: dict[str, str | dict[str, Any]] | None = None
    capabilities: list[str] = Field(default_factory=list)

    @field_validator("operations", "required_args", "optional_args", "capabilities")
    @classmethod
    def _string_lists(cls, value: list[str]) -> list[str]:
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item.strip() for item in value
        ):
            raise ValueError("must be a list of non-empty strings")
        return value

    @field_validator("required_args", "optional_args")
    @classmethod
    def _identifiers(cls, value: list[str]) -> list[str]:
        import keyword

        if any(not item.isidentifier() or keyword.iskeyword(item) for item in value):
            raise ValueError("must contain valid Python identifiers")
        return value

    @field_validator("optional_args")
    @classmethod
    def _no_overlap(cls, value: list[str], info: Any) -> list[str]:
        required = info.data.get("required_args", [])
        overlap = sorted(set(required) & set(value))
        if overlap:
            raise ValueError(f"overlaps required_args: {overlap}")
        return value

    @field_validator("argument_types")
    @classmethod
    def _argument_type_values(
        cls, value: dict[str, str | dict[str, Any]]
    ) -> dict[str, str | dict[str, Any]]:
        if not isinstance(value, dict) or any(
            not isinstance(key, str) or not key.isidentifier() for key in value
        ):
            raise ValueError("must be a mapping with identifier keys")
        return value

    @model_validator(mode="after")
    def _declared_argument_types(self) -> ToolContractModel:
        declared = set(self.required_args) | set(self.optional_args)
        unknown = sorted(set(self.argument_types) - declared)
        if unknown:
            raise ValueError(f"argument_types contains undeclared arguments: {unknown}")
        return self


class ToolConfigModel(_ConfigModel):
    """Configuration for one application tool."""

    callable: str | None = Field(
        default=None,
        description="Import path in package.module:function form.",
        json_schema_extra={
            "pattern": (
                r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*"
                r"[A-Za-z_][A-Za-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*$"
            )
        },
    )
    protect: dict[str, Any] | None = None
    bounded: dict[str, Any] | None = None
    ledger: bool | dict[str, Any] | None = None
    audit_receipt: bool = False
    side_effect_class: str | None = Field(
        default=None,
        json_schema_extra={
            "enum": [
                "read",
                "idempotent_mutate",
                "keyed_mutate",
                "non_idempotent_mutate",
                "irreversible",
                "read_only",
                "idempotent_write",
                "external_api_mutation",
                "non_idempotent_write",
                "payment",
                "email",
                "subagent",
                "onchain_action",
                None,
            ]
        },
    )
    retry_permission: str | None = Field(
        default=None,
        json_schema_extra={
            "enum": [
                "safe_retry",
                "retry_only_with_same_provider_idempotency_key",
                "manual_reconciliation_required",
                "never_retry_automatically",
                None,
            ]
        },
    )
    side_effect_boundary: str | None = Field(
        default=None,
        json_schema_extra={"enum": ["not_crossed", "maybe_crossed", "crossed", None]},
    )
    spendability: str | None = Field(
        default=None,
        json_schema_extra={"enum": ["multi_use", "single_use", "non_replayable", None]},
    )
    capability: str | None = Field(
        default=None,
        json_schema_extra={"enum": ["idempotent", "queryable", "blind", None]},
    )
    provider_idempotency_key_param: str | None = None
    provider_idempotency_key_ttl: float | None = Field(default=None, gt=0)
    propagate_effect_id_as_provider_key: bool = False
    request_id_from: str | None = None
    loop_guard: bool | dict[str, Any] | None = None
    budget_guard: bool | None = None
    scope_guard: bool | dict[str, Any] | None = None
    state_authority: bool | dict[str, Any] | None = None
    secret_fields: list[str] = Field(default_factory=list)
    secret_args: bool | None = None
    entity_guard: bool | None = None
    destructive_confirm: bool | None = None
    use_time_currency: bool | None = None
    # Standardized contract fields are accepted directly for ergonomic YAML.
    operations: list[str] | None = None
    required_args: list[str] | None = None
    optional_args: list[str] | None = None
    argument_types: dict[str, str | dict[str, Any]] | None = None
    output_schema: dict[str, str | dict[str, Any]] | None = None
    capabilities: list[str] | None = None


class TaskConfigModel(_ConfigModel):
    """Configuration for one application task."""

    callable: str | None = Field(
        default=None,
        json_schema_extra={
            "pattern": (
                r"^(?:[A-Za-z_][A-Za-z0-9_]*\.)*"
                r"[A-Za-z_][A-Za-z0-9_]*:[A-Za-z_][A-Za-z0-9_]*$"
            )
        },
    )
    ledger: bool | dict[str, Any] | None = None
    id_from: list[str] | None = None
    audit_receipt: bool = False


class RegistryConfigModel(_ConfigModel):
    allowed: list[str] = Field(default_factory=list)
    auto: bool = False


class RunnerConfigModel(_ConfigModel):
    max_llm_retries: int | None = Field(default=None, ge=0)
    max_tool_retries: int | None = Field(default=None, ge=0)


class HistoryGuardConfigModel(_ConfigModel):
    """History limits. Omit this section to disable; enabled is unsupported."""

    model_config = ConfigDict(extra="forbid", strict=True)
    max_tokens: int | None = Field(default=None, gt=0, description="Positive token limit.")
    max_messages: int | None = Field(default=None, gt=0, description="Positive message limit.")
    warn_at: float = Field(
        default=0.9,
        gt=0,
        le=1,
        allow_inf_nan=False,
        description="Finite fraction in (0, 1] of the token limit.",
    )
    detect_duplicates: bool = Field(default=True, description="Requires an unquoted YAML boolean.")


class MessageValidatorConfigModel(_ConfigModel):
    enabled: bool = True


class LangGraphIntegrationConfigModel(_ConfigModel):
    enabled: bool = True


class CrewAIIntegrationConfigModel(_ConfigModel):
    enabled: bool = True
    run_id_from: str | None = Field(
        default=None,
        min_length=1,
        description=(
            "Stable Crew.kickoff inputs key used as the run scope. If omitted, "
            "development identity is derived from the crew and full input mapping."
        ),
    )


class IntegrationsConfigModel(_ConfigModel):
    langgraph: bool | LangGraphIntegrationConfigModel | None = None
    crewai: bool | CrewAIIntegrationConfigModel | None = None


class DeploymentConfigModel(_ConfigModel):
    topology: str | None = Field(
        default=None,
        json_schema_extra={"enum": ["single_node", "multi_node", None]},
    )


class SecretArgsConfigModel(_ConfigModel):
    enabled: bool = True
    policy: str = Field(
        default="error",
        json_schema_extra={"enum": ["error", "redact", "warn"]},
    )
    allow_fields: list[str] = Field(default_factory=list)
    allow_tools: list[str] = Field(default_factory=list)
    entropy_detection: bool = True


class EntityAllowConfigModel(_ConfigModel):
    addresses: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    hosts: list[str] = Field(default_factory=list)
    values: list[str] = Field(default_factory=list)


class EntityDestinationConfigModel(_ConfigModel):
    path: str = Field(description="Argument path containing the destination.")
    type: Literal["email", "https_url", "entity_id", "host"]
    allow: EntityAllowConfigModel = Field(default_factory=EntityAllowConfigModel)
    required: bool = True
    reject_redirects: bool = True

    @field_validator("allow", mode="before")
    @classmethod
    def _normalize_empty_allow(cls, value: Any) -> Any:
        # The semantic parser has always accepted these empty legacy forms.
        return {} if value is None or value == [] else value


class EntityToolConfigModel(_ConfigModel):
    destinations: list[EntityDestinationConfigModel]


class EntityGuardConfigModel(_ConfigModel):
    """Destination allowlists. See the SDK reference for transport limitations."""

    enabled: bool = True
    missing_policy: Literal["error", "warn"] = "error"
    policy_version: str | None = None
    tools: dict[str, EntityToolConfigModel] = Field(default_factory=dict)


class AuthorityWindowConfigModel(_ConfigModel):
    """Check host-issued authority expiry immediately before execution."""

    enabled: bool = True
    use_time_check: Literal["required", "optional"] = "required"
    clock_skew_tolerance_seconds: float = Field(default=0, ge=0, allow_inf_nan=False)


class FactSubjectConfigModel(_ConfigModel):
    type: str
    id_from: str
    tenant_from: str | None = None
    account_from: str | None = None


class UseTimeFactConfigModel(_ConfigModel):
    name: str
    subject: FactSubjectConfigModel
    validator: str = Field(description="Name of a host-registered current-fact validator.")
    require: dict[str, Any] | None = None
    revision_from: str | None = None
    max_age_seconds: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    bind_request_id: bool = False
    bind_run_id: bool = False
    bind_thread_id: bool = False
    compare_to_arg: str | None = None
    provider_precondition: str | None = None


class UseTimeToolConfigModel(_ConfigModel):
    facts: list[UseTimeFactConfigModel]


class UseTimeCurrencyConfigModel(_ConfigModel):
    """Revalidate host-bound facts at use time; host validators supply current evidence."""

    enabled: bool = True
    missing_policy: Literal["error", "warn"] = "error"
    policy_version: str | None = None
    tools: dict[str, UseTimeToolConfigModel] = Field(default_factory=dict)


class DestructiveObjectConfigModel(FactSubjectConfigModel):
    case_sensitive: bool = True
    require_canonicalizer: bool = False


class DestructiveGrantConfigModel(_ConfigModel):
    bind_request_id: bool = False
    bind_run_id: bool = False
    bind_thread_id: bool = False
    max_uses: int = Field(default=1, gt=0)
    ttl_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)


class DestructiveToolConfigModel(_ConfigModel):
    operation: str
    object: DestructiveObjectConfigModel
    grant: DestructiveGrantConfigModel = Field(default_factory=DestructiveGrantConfigModel)


class DestructiveConfirmConfigModel(_ConfigModel):
    """Require a host-issued grant bound to the exact destructive action and object."""

    enabled: bool = True
    missing_policy: Literal["error", "warn"] = "error"
    policy_version: str | None = None
    storage: Literal["memory", "file", "sqlite", "redis", "postgres"] = "memory"
    path: str | None = None
    table: str | None = None
    url: str | None = None
    url_env: str | None = None
    dsn: str | None = None
    dsn_env: str | None = None
    prefix: str | None = None
    tools: dict[str, DestructiveToolConfigModel] = Field(default_factory=dict)


class MyceliumConfigModel(_ConfigModel):
    """Version 1 structural model for a complete ``mycelium.yaml`` file.

    Unknown fields remain accepted for compatibility with existing extension
    sections.  Declared fields are strict, documented, and discoverable by
    JSON-Schema-aware editors and configuration agents.
    """

    model_config = ConfigDict(
        extra="allow",
        strict=True,
        title="Mycelium configuration",
        json_schema_extra={
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": CONFIG_SCHEMA_ID,
        },
    )

    config_version: Literal[1] = Field(
        default=CONFIG_VERSION,
        description="Configuration document format. Omitted legacy files are version 1.",
    )
    profile: str = Field(
        default="development",
        json_schema_extra={"enum": ["development", "production"]},
    )
    transition: TransitionConfigModel | None = None
    state_backend: StorageConfigModel | None = None
    action_ledger: LedgerConfigModel | None = None
    task_ledger: TaskLedgerConfigModel | None = None
    state_flush: StorageConfigModel | None = None
    audit_receipt: StorageConfigModel | None = None
    outcome_emit: StorageConfigModel | None = None
    tools: dict[str, ToolConfigModel | None] = Field(default_factory=dict)
    tasks: dict[str, TaskConfigModel | None] = Field(default_factory=dict)
    registry: RegistryConfigModel = Field(default_factory=RegistryConfigModel)
    runner: RunnerConfigModel = Field(default_factory=RunnerConfigModel)
    history_guard: HistoryGuardConfigModel | None = None
    message_validator: bool | MessageValidatorConfigModel = False
    integrations: IntegrationsConfigModel | None = None
    loop_guard: LoopGuardConfigModel | None = None
    budget: BudgetConfigModel | None = None
    scope_guard: ScopeGuardConfigModel | None = None
    state_authority: StateAuthorityConfigModel | None = None
    completion: CompletionConfigModel | None = None
    deployment: DeploymentConfigModel | None = None
    verify: dict[str, Any] | None = None
    secret_args: SecretArgsConfigModel | None = None
    entity_guard: EntityGuardConfigModel | None = None
    destructive_confirm: DestructiveConfirmConfigModel | None = None
    authority_window: AuthorityWindowConfigModel | None = None
    use_time_currency: UseTimeCurrencyConfigModel | None = None

    @field_validator("config_version", mode="before")
    @classmethod
    def _supported_config_version(cls, value: Any) -> Any:
        if value != CONFIG_VERSION:
            raise ValueError(
                f"unsupported config_version {value!r}; this Mycelium "
                f"runtime supports version {CONFIG_VERSION}. Upgrade Mycelium "
                "or migrate the file after reviewing the release notes"
            )
        return value


_STRICT_GUARD_MODELS = {
    "budget": BudgetConfigModel,
    "completion": CompletionConfigModel,
    "loop_guard": LoopGuardConfigModel,
    "scope_guard": ScopeGuardConfigModel,
    "history_guard": HistoryGuardConfigModel,
}


def config_json_schema() -> dict[str, Any]:
    """Return the versioned JSON Schema used by agents and IDEs."""

    return MyceliumConfigModel.model_json_schema(mode="validation")


def validate_config_shape(data: Any) -> MyceliumConfigModel:
    """Validate version and declared field shapes without constructing runtime guards."""

    return MyceliumConfigModel.model_validate(data)


def format_validation_error(exc: ValidationError) -> str:
    """Render stable, YAML-oriented diagnostics from a Pydantic error."""

    diagnostics: list[str] = []
    for error in exc.errors(include_url=False):
        location = ""
        for part in error["loc"]:
            if isinstance(part, int):
                location += f"[{part}]"
            else:
                location += ("." if location else "") + str(part)
        diagnostics.append(f"{location or '<root>'}: {error['msg']}")
    return "; ".join(diagnostics)


__all__ = [
    "CONFIG_SCHEMA_ID",
    "CONFIG_VERSION",
    "MyceliumConfigModel",
    "ToolContractModel",
    "config_json_schema",
    "format_validation_error",
    "validate_config_shape",
]
