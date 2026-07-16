"""Transition envelope: rich idempotency keys for side-effecting tools."""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import AbstractContextManager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from mycelium._compat import StrEnum

TRANSITION_SCHEMA = "mycelium.transition/v1"

SCOPE_FIELDS = ("thread_id", "run_id", "node")

LEDGER_KWARG_KEYS = frozenset(
    {"request_id", "tool_call_id", "thread_id", "run_id", "node"}
)


class SideEffectClass(StrEnum):
    """Per-tool side-effect classification for retry/redispatch policy.

    Classes describe *effect semantics*, not business domains:

    - ``read`` — no external mutation
    - ``idempotent_mutate`` — mutation; retry-safe as-is
    - ``keyed_mutate`` — safe only with the same provider idempotency key
    - ``non_idempotent_mutate`` — second call = second effect
    - ``irreversible`` — no compensation; ambiguity requires human reconcile
    """

    READ = "read"
    IDEMPOTENT_MUTATE = "idempotent_mutate"
    KEYED_MUTATE = "keyed_mutate"
    NON_IDEMPOTENT_MUTATE = "non_idempotent_mutate"
    IRREVERSIBLE = "irreversible"


# Legacy YAML / API names accepted by :func:`parse_side_effect_class`.
SIDE_EFFECT_CLASS_ALIASES: dict[str, SideEffectClass] = {
    "read_only": SideEffectClass.READ,
    "idempotent_write": SideEffectClass.IDEMPOTENT_MUTATE,
    "external_api_mutation": SideEffectClass.KEYED_MUTATE,
    "non_idempotent_write": SideEffectClass.NON_IDEMPOTENT_MUTATE,
    "payment": SideEffectClass.NON_IDEMPOTENT_MUTATE,
    "email": SideEffectClass.NON_IDEMPOTENT_MUTATE,
    "subagent": SideEffectClass.NON_IDEMPOTENT_MUTATE,
    "onchain_action": SideEffectClass.IRREVERSIBLE,
}


class TerminalOutcome(StrEnum):
    """Terminal or in-progress state of a side-effect transition."""

    IN_FLIGHT = "IN_FLIGHT"
    COMPLETED = "COMPLETED"
    FAILED_BEFORE_EFFECT = "FAILED_BEFORE_EFFECT"
    FAILED_AFTER_EFFECT = "FAILED_AFTER_EFFECT"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    UNKNOWN = "UNKNOWN"


class SideEffectBoundary(StrEnum):
    """Whether an external side-effect boundary was crossed."""

    NOT_CROSSED = "not_crossed"
    MAYBE_CROSSED = "maybe_crossed"
    CROSSED = "crossed"


class RetryPermission(StrEnum):
    """Whether an automatic retry/redispatch is permitted."""

    SAFE_RETRY = "safe_retry"
    RETRY_ONLY_WITH_SAME_PROVIDER_IDEMPOTENCY_KEY = (
        "retry_only_with_same_provider_idempotency_key"
    )
    MANUAL_RECONCILIATION_REQUIRED = "manual_reconciliation_required"
    NEVER_RETRY_AUTOMATICALLY = "never_retry_automatically"


DEFAULT_RETRY_PERMISSION: dict[SideEffectClass, RetryPermission] = {
    SideEffectClass.READ: RetryPermission.SAFE_RETRY,
    SideEffectClass.IDEMPOTENT_MUTATE: RetryPermission.SAFE_RETRY,
    SideEffectClass.KEYED_MUTATE: (
        RetryPermission.RETRY_ONLY_WITH_SAME_PROVIDER_IDEMPOTENCY_KEY
    ),
    SideEffectClass.NON_IDEMPOTENT_MUTATE: (
        RetryPermission.MANUAL_RECONCILIATION_REQUIRED
    ),
    SideEffectClass.IRREVERSIBLE: RetryPermission.NEVER_RETRY_AUTOMATICALLY,
}


STRICT_SIDE_EFFECT_CLASSES = frozenset(
    {
        SideEffectClass.NON_IDEMPOTENT_MUTATE,
        SideEffectClass.IRREVERSIBLE,
    }
)


def is_strict_side_effect(side_effect_class: SideEffectClass) -> bool:
    """Return whether a class requires strict hard-block-on-ambiguity resolution."""
    return side_effect_class in STRICT_SIDE_EFFECT_CLASSES


def allows_failed_before_retry(side_effect_class: SideEffectClass) -> bool:
    """Whether ``FAILED_BEFORE_EFFECT`` may be automatically retried."""
    return resolve_retry_permission(side_effect_class, None) in (
        RetryPermission.SAFE_RETRY,
        RetryPermission.RETRY_ONLY_WITH_SAME_PROVIDER_IDEMPOTENCY_KEY,
    )


def parse_side_effect_boundary(value: Any) -> SideEffectBoundary:
    if not isinstance(value, str):
        raise ValueError("side_effect_boundary must be a string")
    try:
        return SideEffectBoundary(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in SideEffectBoundary)
        raise ValueError(
            f"invalid side_effect_boundary {value!r}; expected one of: {allowed}"
        ) from exc


def parse_retry_permission(value: Any) -> RetryPermission:
    if not isinstance(value, str):
        raise ValueError("retry_permission must be a string")
    try:
        return RetryPermission(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in RetryPermission)
        raise ValueError(
            f"invalid retry_permission {value!r}; expected one of: {allowed}"
        ) from exc


def resolve_retry_permission(
    side_effect_class: SideEffectClass,
    explicit: RetryPermission | None,
) -> RetryPermission:
    if explicit is not None:
        return explicit
    return DEFAULT_RETRY_PERMISSION[side_effect_class]


def resolve_side_effect_boundary_default(
    explicit: SideEffectBoundary | None,
) -> SideEffectBoundary:
    if explicit is not None:
        return explicit
    return SideEffectBoundary.NOT_CROSSED


def parse_terminal_outcome(value: Any) -> TerminalOutcome:
    if isinstance(value, TerminalOutcome):
        return value
    if not isinstance(value, str):
        raise ValueError("terminal_outcome must be a string")
    try:
        return TerminalOutcome(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in TerminalOutcome)
        raise ValueError(
            f"invalid terminal_outcome {value!r}; expected one of: {allowed}"
        ) from exc


def terminal_from_legacy_status(
    status: str,
    *,
    lease_until: float | None = None,
    now: float | None = None,
) -> TerminalOutcome:
    """Infer ``terminal_outcome`` from legacy v1.2 ``status`` values."""
    if status == "completed":
        return TerminalOutcome.COMPLETED
    if status == "failed":
        return TerminalOutcome.FAILED_BEFORE_EFFECT
    if status == "in-flight":
        if lease_until is not None:
            now = now if now is not None else time.time()
            if now >= lease_until:
                return TerminalOutcome.EXPIRED
        return TerminalOutcome.IN_FLIGHT
    return TerminalOutcome.UNKNOWN


def legacy_status_from_terminal(terminal_outcome: TerminalOutcome) -> str:
    """Map ``terminal_outcome`` to legacy ``status`` for backward compatibility."""
    if terminal_outcome == TerminalOutcome.COMPLETED:
        return "completed"
    if terminal_outcome in (
        TerminalOutcome.FAILED_BEFORE_EFFECT,
        TerminalOutcome.FAILED_AFTER_EFFECT,
        TerminalOutcome.BLOCKED,
        TerminalOutcome.UNKNOWN,
    ):
        return "failed"
    return "in-flight"


def resolve_terminal_outcome(
    terminal_outcome: TerminalOutcome | str,
    *,
    lease_until: float | None,
    now: float | None = None,
) -> TerminalOutcome:
    """Return the effective terminal outcome, treating stale leases as ``EXPIRED``."""
    outcome = (
        terminal_outcome
        if isinstance(terminal_outcome, TerminalOutcome)
        else parse_terminal_outcome(terminal_outcome)
    )
    if outcome == TerminalOutcome.IN_FLIGHT and lease_until is not None:
        now = now if now is not None else time.time()
        if now >= lease_until:
            return TerminalOutcome.EXPIRED
    return outcome


@dataclass(frozen=True)
class TransitionConfig:
    """Deployment-level transition settings from YAML ``transition:``."""

    agent_id: str
    policy_version: str
    scope_from: dict[str, str] = field(default_factory=dict)
    lease_ttl: float | None = None
    poll_interval: float | None = None
    poll_timeout: float | None = None


@dataclass(frozen=True)
class ToolTransitionBinding:
    """Per-tool binding used when deriving a transition key at runtime."""

    agent_id: str
    policy_version: str
    side_effect_class: SideEffectClass
    scope_from: dict[str, str] = field(default_factory=dict)
    retry_permission: RetryPermission = RetryPermission.MANUAL_RECONCILIATION_REQUIRED
    side_effect_boundary_default: SideEffectBoundary = SideEffectBoundary.NOT_CROSSED

    @classmethod
    def for_tool(
        cls,
        *,
        agent_id: str,
        policy_version: str,
        side_effect_class: SideEffectClass,
        scope_from: dict[str, str] | None = None,
        retry_permission: RetryPermission | None = None,
        side_effect_boundary: SideEffectBoundary | None = None,
    ) -> ToolTransitionBinding:
        return cls(
            agent_id=agent_id,
            policy_version=policy_version,
            side_effect_class=side_effect_class,
            scope_from=dict(scope_from or {}),
            retry_permission=resolve_retry_permission(
                side_effect_class, retry_permission
            ),
            side_effect_boundary_default=resolve_side_effect_boundary_default(
                side_effect_boundary
            ),
        )


@dataclass(frozen=True)
class TransitionScope:
    """Execution scope for a single agent run / graph step."""

    thread_id: str = ""
    run_id: str = ""
    node: str = ""


_execution_scope_var: ContextVar[TransitionScope | None] = ContextVar(
    "mycelium_execution_scope",
    default=None,
)


def get_active_execution_scope() -> TransitionScope | None:
    """Return the active execution scope, if any."""
    return _execution_scope_var.get()


def execution_scope(scope: TransitionScope) -> AbstractContextManager[TransitionScope]:
    """Context manager that sets the active execution scope."""
    return _ExecutionScopeContext(scope)


class _ExecutionScopeContext(AbstractContextManager[TransitionScope]):
    def __init__(self, scope: TransitionScope) -> None:
        self._scope = scope
        self._token: Token[TransitionScope | None] | None = None

    def __enter__(self) -> TransitionScope:
        self._token = _execution_scope_var.set(self._scope)
        return self._scope

    def __exit__(self, *_: Any) -> bool:
        if self._token is not None:
            _execution_scope_var.reset(self._token)
            self._token = None
        return False


def parse_side_effect_class(value: Any) -> SideEffectClass:
    """Parse and validate a side_effect_class value from YAML.

    Accepts the five canonical classes and legacy aliases
    (``read_only``, ``payment``, ``subagent``, …).
    """
    if not isinstance(value, str):
        raise ValueError("side_effect_class must be a string")
    alias = SIDE_EFFECT_CLASS_ALIASES.get(value)
    if alias is not None:
        return alias
    try:
        return SideEffectClass(value)
    except ValueError as exc:
        allowed = ", ".join(member.value for member in SideEffectClass)
        raise ValueError(
            f"invalid side_effect_class {value!r}; expected one of: {allowed}"
        ) from exc


def canonical_json(payload: dict[str, Any]) -> str:
    """Serialize a mapping to deterministic JSON for hashing."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _tool_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in kwargs.items() if key not in LEDGER_KWARG_KEYS}


def args_fingerprint(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    """Hash canonical tool arguments, excluding Mycelium bookkeeping keys."""
    payload = {"args": args, "kwargs": _tool_kwargs(kwargs)}
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def derive_dispatch_id(kwargs: dict[str, Any]) -> str | None:
    """Return dispatch identity when the caller or framework supplies one."""
    if "tool_call_id" in kwargs:
        return str(kwargs["tool_call_id"])
    if "request_id" in kwargs:
        return str(kwargs["request_id"])
    return None


def resolve_scope(
    *,
    scope_from: dict[str, str],
    kwargs: dict[str, Any],
) -> TransitionScope:
    """Merge active execution scope with kwargs and configured bindings."""
    base = get_active_execution_scope() or TransitionScope()
    resolved = {
        "thread_id": base.thread_id,
        "run_id": base.run_id,
        "node": base.node,
    }
    for field_name, source in scope_from.items():
        if field_name not in SCOPE_FIELDS:
            continue
        if source in kwargs:
            resolved[field_name] = str(kwargs[source])
    for field_name in SCOPE_FIELDS:
        if field_name in kwargs:
            resolved[field_name] = str(kwargs[field_name])
    return TransitionScope(**resolved)


def build_transition_preimage(
    *,
    scope: TransitionScope,
    dispatch_id: str | None,
    tool: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    side_effect_class: SideEffectClass,
    agent_id: str,
    policy_version: str,
) -> dict[str, Any]:
    """Build the versioned preimage hashed into a transition key."""
    preimage: dict[str, Any] = {
        "schema": TRANSITION_SCHEMA,
        "scope": {
            "thread_id": scope.thread_id,
            "run_id": scope.run_id,
            "node": scope.node,
        },
        "tool": tool,
        "args_fingerprint": args_fingerprint(args, kwargs),
        "side_effect_class": side_effect_class.value,
        "agent_id": agent_id,
        "policy_version": policy_version,
    }
    if dispatch_id is not None:
        preimage["dispatch_id"] = dispatch_id
    return preimage


def derive_transition_key(preimage: dict[str, Any]) -> str:
    """Hash a transition preimage into a durable transition key."""
    return hashlib.sha256(canonical_json(preimage).encode()).hexdigest()


def derive_transition_key_for_call(
    tool: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    binding: ToolTransitionBinding,
) -> str:
    """Derive the transition key for a tool invocation."""
    scope = resolve_scope(scope_from=binding.scope_from, kwargs=kwargs)
    dispatch_id = derive_dispatch_id(kwargs)
    preimage = build_transition_preimage(
        scope=scope,
        dispatch_id=dispatch_id,
        tool=tool,
        args=args,
        kwargs=kwargs,
        side_effect_class=binding.side_effect_class,
        agent_id=binding.agent_id,
        policy_version=binding.policy_version,
    )
    return derive_transition_key(preimage)


__all__ = [
    "LEDGER_KWARG_KEYS",
    "SCOPE_FIELDS",
    "SIDE_EFFECT_CLASS_ALIASES",
    "TRANSITION_SCHEMA",
    "SideEffectClass",
    "SideEffectBoundary",
    "RetryPermission",
    "DEFAULT_RETRY_PERMISSION",
    "STRICT_SIDE_EFFECT_CLASSES",
    "TerminalOutcome",
    "ToolTransitionBinding",
    "TransitionConfig",
    "TransitionScope",
    "args_fingerprint",
    "build_transition_preimage",
    "canonical_json",
    "derive_dispatch_id",
    "derive_transition_key",
    "derive_transition_key_for_call",
    "execution_scope",
    "get_active_execution_scope",
    "legacy_status_from_terminal",
    "parse_side_effect_class",
    "parse_retry_permission",
    "parse_side_effect_boundary",
    "parse_terminal_outcome",
    "resolve_retry_permission",
    "resolve_side_effect_boundary_default",
    "resolve_scope",
    "resolve_terminal_outcome",
    "allows_failed_before_retry",
    "is_strict_side_effect",
    "terminal_from_legacy_status",
]
