"""Scenario registry for ``mycelium verify``."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mycelium.verify.isolation import IsolationSession
    from mycelium.verify.types import VerificationEvidence

SCENARIO_ORDER = (
    "redispatch",
    "contention",
    "storage-outage",
    "worker-crash",
    "ambiguous-effect",
    "reconcile",
    "secret-in-args",
    "entity-guard",
    "destructive-confirm",
    "authority-window",
    "use-time-currency",
    "simulation",
    "state-machine-exhaustive",
    "effect-protocol-proof",
    "trust-boundary",
)

ScenarioFn = Callable[["ScenarioContext"], "VerificationEvidence"]

_REGISTRY: dict[str, ScenarioFn] = {}


@dataclass
class ScenarioContext:
    isolation: IsolationSession
    timeout_seconds: float
    rounds: int
    workers: int
    keep_artifacts: bool
    owned_procs: list[Any] = field(default_factory=list)


def register_scenario(name: str, fn: ScenarioFn) -> ScenarioFn:
    _REGISTRY[name] = fn
    return fn


def verify_scenario(name: str) -> Callable[[ScenarioFn], ScenarioFn]:
    def decorator(fn: ScenarioFn) -> ScenarioFn:
        return register_scenario(name, fn)

    return decorator


def get_scenario(name: str) -> ScenarioFn | None:
    return _REGISTRY.get(name)


def known_scenarios() -> tuple[str, ...]:
    return SCENARIO_ORDER


def resolve_scenario_names(selected: list[str]) -> list[str]:
    names: list[str] = []
    for raw in selected:
        if raw == "all":
            for item in SCENARIO_ORDER:
                if item not in names:
                    names.append(item)
            continue
        if raw not in SCENARIO_ORDER:
            raise ValueError(
                f"unknown scenario {raw!r}; choose from {list(SCENARIO_ORDER)} or 'all'"
            )
        if raw not in names:
            names.append(raw)
    return names


def ensure_builtin_scenarios_registered() -> None:
    from mycelium.verify.scenarios import (  # noqa: F401
        ambiguous_effect,
        authority_window,
        contention,
        destructive_confirm,
        effect_protocol_proof,
        entity_guard,
        reconcile,
        redispatch,
        secret_in_args,
        simulation,
        state_machine_exhaustive,
        storage_outage,
        trust_boundary,
        use_time_currency,
        worker_crash,
    )


__all__ = [
    "SCENARIO_ORDER",
    "ScenarioContext",
    "ensure_builtin_scenarios_registered",
    "get_scenario",
    "known_scenarios",
    "register_scenario",
    "resolve_scenario_names",
    "verify_scenario",
]
