"""Protection inventory without importing or executing application tools."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from mycelium.config import _request_identity_policy
from mycelium.runtime_builder import _GUARD_MARKERS
from mycelium.transition import CONSEQUENTIAL_SIDE_EFFECT_CLASSES

if TYPE_CHECKING:
    from mycelium.config import MyceliumConfig


def protection_coverage(
    config: MyceliumConfig,
    observed_tools: Mapping[str, Callable[..., Any]] | None = None,
) -> list[dict[str, Any]]:
    """Separate configuration evidence from optional host-supplied wrapper observations.

    The host supplies the exact callables used by its application. Marker
    observation does not prove hidden effects or provider boundary placement.
    """
    observed = observed_tools or {}
    rows: list[dict[str, Any]] = []
    for name in sorted(set(config.tools) | set(config.registry_allowed) | set(observed)):
        tool = config.tools.get(name)
        side_effect_class = tool.side_effect_class if tool else None
        consequential = side_effect_class in CONSEQUENTIAL_SIDE_EFFECT_CLASSES
        ledger = tool.ledger if tool else None
        storage = str(ledger.get("storage", "memory")) if ledger is not None else None
        markers = []
        if name in observed:
            markers = [
                marker for marker in _GUARD_MARKERS if getattr(observed[name], marker, False)
            ]
        wrapper = (
            "unverified"
            if name not in observed
            else ("wrapper_observed" if markers else "unprotected")
        )
        identity = "unconfigured"
        if ledger is not None:
            if tool.request_id_from:
                identity = "host_field_configured"
            elif (
                _request_identity_policy(config.action_ledger, profile=config.profile)
                == "require_explicit"
            ):
                identity = "explicit_request_required"
            else:
                identity = "derived_or_host"
        configured_controls: list[str] = []
        if tool is not None:
            for control in ("ledger", "bounded", "protect", "contract"):
                if getattr(tool, control) is not None:
                    configured_controls.append(control)
            for control in (
                "loop_guard",
                "budget_guard",
                "scope_guard",
                "state_authority",
                "secret_args",
                "entity_guard",
                "destructive_confirm",
                "use_time_currency",
            ):
                if getattr(config, f"{control}_applies")(name, tool):
                    configured_controls.append(control)
        missing: list[str] = []
        if tool is None:
            missing.append("tool_configuration")
        if side_effect_class is None:
            missing.append("effect_classification")
        if wrapper != "wrapper_observed":
            missing.append("wrapper_observation" if wrapper == "unverified" else "tool_wrapper")
        if consequential:
            if storage is None or storage == "memory":
                missing.append("durable_ledger")
            if identity not in ("host_field_configured", "explicit_request_required"):
                missing.append("host_owned_identity")
            if "_mycelium_ledger" not in markers:
                missing.append("ledger_wrapper_observation")
            missing.extend(("provider_boundary_verification", "recovery_path_verification"))
        rows.append(
            {
                "tool": name,
                "configured": tool is not None,
                "configured_controls": configured_controls,
                "wrapper_status": wrapper,
                "observed_wrapper_markers": markers,
                "side_effect_class": side_effect_class.value
                if side_effect_class
                else "unclassified",
                "ledger_storage": storage,
                "durable_ledger_configured": storage is not None and storage != "memory",
                "request_identity": identity,
                "provider_boundary": "unverified",
                "recovery_path": "unverified",
                "missing_requirements": missing,
            }
        )
    return rows
