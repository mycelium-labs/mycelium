"""Policy configuration.

A Policy declares which protections are active, how strictly each is enforced,
and the budgets / manifests used by the runtime. In v1, policy is set at
``protect(...)`` time; later versions will allow per-environment loading from
YAML and remote fetch from the cloud control plane.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from mycelium.core.types import Capability, Enforcement, FailureMode


class PolicyPreset(str, Enum):
    """Named starting points. Each preset is just a bundle of defaults that
    customers then override per agent.
    """

    STRICT = "strict"  # BLOCK everywhere, minimal surface area
    BALANCED = "balanced"  # BLOCK on the dangerous modes, FLAG on soft ones
    PERMISSIVE = "permissive"  # FLAG everywhere — observe-only, no enforcement


@dataclass
class Policy:
    """Runtime configuration for a protected agent.

    The default (``Policy()``) is observe-only: every protection runs, every
    finding is logged as an incident, nothing is blocked. This is the right
    starting point for dogfooding and for customer-pilot "shadow mode"
    deployments.
    """

    enforcement: dict[FailureMode, Enforcement] = field(default_factory=dict)
    capabilities: frozenset[Capability] = field(default_factory=frozenset)
    goals: tuple[str, ...] = ()
    max_steps: int = 50
    max_tool_calls: int = 200
    loop_window: int = 5  # action-hash ring buffer size
    stale_context_ttl_seconds: int = 300

    @classmethod
    def from_preset(cls, preset: PolicyPreset | str) -> Policy:
        preset = PolicyPreset(preset) if isinstance(preset, str) else preset
        if preset is PolicyPreset.STRICT:
            return cls(
                enforcement={mode: Enforcement.BLOCK for mode in FailureMode},
            )
        if preset is PolicyPreset.BALANCED:
            block = {
                FailureMode.TOOL_MISUSE,
                FailureMode.CASCADING_PERMISSION,
                FailureMode.INSTRUCTION_INJECTION,
                FailureMode.PREMATURE_TERMINATION,
            }
            return cls(
                enforcement={
                    mode: (Enforcement.BLOCK if mode in block else Enforcement.FLAG)
                    for mode in FailureMode
                },
            )
        return cls(
            enforcement={mode: Enforcement.FLAG for mode in FailureMode},
        )

    def enforcement_for(self, mode: FailureMode) -> Enforcement:
        return self.enforcement.get(mode, Enforcement.FLAG)
