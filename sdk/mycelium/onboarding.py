"""Read-only onboarding reports for protection coverage and action previews.

This module deliberately does not import configured callables.  It consumes the
parsed YAML metadata and the same pure contract/entity validators used by the
runtime, leaving claims, grants, budgets, and provider calls to execution.
"""
# Report strings are intentionally kept readable.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from mycelium.config import (
    MyceliumConfig,
    _load_config_for_preflight,
    entity_guard_policy_from_mapping,
)
from mycelium.contracts import validate_contract_call
from mycelium.entity_guard import EntityGuardError, enforce_entity_guard
from mycelium.secret_protection import sanitize_secrets

REPORT_SCHEMA = "mycelium.onboarding-report/v1"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    reason: str
    explanation: str


def _config_digest(config: MyceliumConfig) -> str:
    raw = {"version": config.config_version, "tools": sorted(config.tools), "profile": config.profile}
    return hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:16]


def coverage_report(config: MyceliumConfig) -> dict[str, Any]:
    protections = (
        ("contract", lambda t: t.contract is not None, "Declare a tool contract"),
        ("ledger", lambda t: t.ledger is not None, "Configure action_ledger or tools.<name>.ledger"),
        ("destination_policy", lambda t: config.entity_guard_applies(t.name, t), "Configure entity_guard destinations"),
        ("secret_protection", lambda t: config.secret_args_applies(t.name, t) or bool(t.secret_fields), "Configure secret_args or secret_fields"),
        ("loop_control", lambda t: config.loop_guard_applies(t.name, t), "Configure loop_guard"),
        ("budget_control", lambda t: config.budget_guard_applies(t.name, t), "Configure budget"),
        ("scope_control", lambda t: config.scope_guard_applies(t.name, t), "Configure scope_guard"),
        ("authority_control", lambda t: config.state_authority_applies(t.name, t) or config.authority_window is not None, "Configure authority/state authority"),
    )
    tools: list[dict[str, Any]] = []
    for name, tool in sorted(config.tools.items()):
        configured = []
        missing = []
        for label, predicate, hint in protections:
            if predicate(tool):
                configured.append(label)
            else:
                missing.append({"protection": label, "reason": "not_configured", "explanation": hint})
        capability = tool.capability.value if tool.capability is not None else "derived_or_unknown"
        reconciliation = "provider_idempotency_key" if tool.provider_idempotency_key_param else (
            "declared_queryable; reconciler binding is runtime-owned" if capability == "queryable" else "none_declared"
        )
        tools.append({
            "tool": name,
            "effect": tool.side_effect_class.value if tool.side_effect_class else "unknown",
            "recovery_capability": capability,
            "configured_protections": configured,
            "missing": missing,
            "reconciliation": {"support": reconciliation, "limitations": "Provider truth and read-only reconciler wiring require runtime evidence."},
            "runtime_integration": {"status": "unknown", "reason": "no associated runtime verification evidence", "explanation": "Configuration presence does not prove every call passes through Mycelium."},
        })
    return {"schema_version": REPORT_SCHEMA, "mode": "coverage", "config_digest": _config_digest(config), "profile": config.profile, "tools": tools}


def _safe_args(value: Any) -> Any:
    return sanitize_secrets(value, entropy_detection=False)


def preview(config: MyceliumConfig, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    preview_id = "preview_" + uuid.uuid4().hex
    checks: list[CheckResult] = []
    tool = config.tools.get(tool_name)
    if tool is None:
        checks.append(CheckResult("tool_resolution", "failed", "unknown_tool", "The tool is not declared in this configuration."))
        decision = "denied"
        summary = "Unknown tool"
        limitations = ["No callable or production action was resolved."]
        return _report(preview_id, config, tool_name, args, summary, checks, decision, limitations)

    normalized = dict(args)
    if tool.contract is not None:
        try:
            normalized = validate_contract_call(tool_name, tool.contract, normalized)
            checks.append(CheckResult("contract", "passed", "contract_valid", "Arguments satisfy the declared tool contract."))
        except Exception as exc:
            checks.append(CheckResult("contract", "failed", "contract_validation_failed", "Arguments do not satisfy the declared contract."))
            return _report(preview_id, config, tool_name, args, "Contract validation failed", checks, "denied", [str(exc).split("actual=", 1)[0].strip()])
    else:
        checks.append(CheckResult("contract", "not_evaluated", "contract_missing", "No declarative contract is configured; runtime validation may still apply."))

    if config.entity_guard_applies(tool_name, tool):
        try:
            policy = entity_guard_policy_from_mapping(config.entity_guard or {})
            _args, normalized, decision = enforce_entity_guard(tool_name, (), normalized, policy=policy)
            destination = ", ".join(item.entity for item in decision.destinations)
            checks.append(CheckResult("destination_policy", "passed", "destination_allowed", "Declared destinations are canonical and allowed."))
        except EntityGuardError as exc:
            checks.append(CheckResult("destination_policy", "failed", getattr(exc, "reason", "destination_denied"), "The destination policy rejected the proposed destination."))
            return _report(preview_id, config, tool_name, args, "Destination denied", checks, "denied", ["Actual execution must re-evaluate destination policy."])
    else:
        destination = None
        checks.append(CheckResult("destination_policy", "not_applicable", "destination_policy_not_configured", "No destination policy applies to this tool."))

    checks.append(CheckResult("execution_boundary", "not_evaluated", "atomic_boundary_required", "Ledger claim, grants, budgets, live authority, and provider state are evaluated only during execution."))
    decision = "indeterminate" if tool.ledger is not None else "permitted_at_preview"
    limitations = ["Preview is a snapshot and is not an approval, reservation, or execution guarantee."]
    if tool.ledger is not None:
        limitations.append("The atomic claim/execution boundary was not simulated; execution must re-evaluate all controls.")
    return _report(preview_id, config, tool_name, normalized, f"Proposed {tool_name} action", checks, decision, limitations, destination=destination)


def _report(preview_id: str, config: MyceliumConfig, tool: str, args: dict[str, Any], summary: str, checks: list[CheckResult], decision: str, limitations: list[str], *, destination: str | None = None) -> dict[str, Any]:
    return sanitize_secrets({
        "schema_version": REPORT_SCHEMA, "mode": "preview", "report_id": preview_id,
        "tool": tool, "action_summary": summary, "destination": destination,
        "arguments": _safe_args(args), "policy_digest": _config_digest(config),
        "checks": [asdict(item) for item in checks], "decision": decision,
        "execution_outcome": "not_executed", "evidence": ["preview_snapshot"],
        "limitations": limitations,
        "next_steps": ["Execute through the configured Mycelium wrapper; do not treat this report as authorization."],
    }, entropy_detection=False)


def load_preflight(path: str | Path) -> MyceliumConfig:
    return _load_config_for_preflight(path)


def execution_report(entry: Any, *, now: float | None = None) -> dict[str, Any]:
    """Project an authoritative ledger entry into the onboarding report shape."""
    resolved = entry.resolved_terminal_outcome(now=now)
    outcome = resolved.value
    normalized_outcome = outcome.casefold()
    if normalized_outcome == "completed":
        decision = "permitted"
        next_steps = ["Use the recorded provider/local result and receipt references."]
    elif normalized_outcome in {"unknown", "in_flight", "expired", "blocked"}:
        decision = "uncertain"
        next_steps = ["Reconcile or use the supported operator resolution path before retrying."]
    else:
        decision = "denied"
        next_steps = ["Address the recorded boundary or policy reason, then submit a fresh action."]
    return sanitize_secrets({
        "schema_version": REPORT_SCHEMA,
        "mode": "execution",
        "report_id": entry.request_id,
        "action_id": entry.effect_id,
        "tool": entry.tool,
        "action_summary": f"Execution of {entry.tool}",
        "destination": None,
        "checks": [{"name": "runtime_decision", "status": "passed" if decision == "permitted" else "failed" if decision == "denied" else "not_evaluated", "reason": entry.decision or "ledger_outcome", "explanation": "Authoritative runtime record."}],
        "decision": decision,
        "execution_outcome": outcome,
        "evidence": [ref for ref in (entry.receipt_ref, entry.external_operation_ref) if ref],
        "limitations": ["Provider confirmation is distinct from local ledger state."],
        "next_steps": next_steps,
    }, entropy_detection=False)


__all__ = ["REPORT_SCHEMA", "coverage_report", "load_preflight", "preview"]
