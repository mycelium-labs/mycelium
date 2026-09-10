"""Subprocess fixture for composite restart verification."""

from __future__ import annotations

import json
import os
from pathlib import Path

from mycelium import (
    FileLedgerStorage,
    SideEffectClass,
    ToolTransitionBinding,
    composite,
    ledger_sync,
    register_composite_helper,
    side_effect,
)

ROOT = Path(os.environ["MYCELIUM_COMPOSITE_FIXTURE_DIR"])
LEDGER = FileLedgerStorage(ROOT / "ledger.json")
COUNTS = ROOT / "provider-counts.json"
CRASH = os.environ.get("MYCELIUM_COMPOSITE_CRASH") == "1"
CRASH_WINDOW = os.environ.get("MYCELIUM_COMPOSITE_CRASH_WINDOW") == "1"


def _binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="fixture",
        policy_version="1",
        side_effect_class=SideEffectClass.KEYED_MUTATE,
        provider_idempotency_key_param="idempotency_key",
    )


def _provider(name: str) -> None:
    current = json.loads(COUNTS.read_text()) if COUNTS.exists() else {}
    current[name] = int(current.get(name, 0)) + 1
    COUNTS.write_text(json.dumps(current))


@ledger_sync(storage=LEDGER, transition_binding=_binding(), lease_ttl=0.05)
def create(idempotency_key: str) -> str:
    with side_effect():
        _provider("A")
    return "commit-1"


@ledger_sync(storage=LEDGER, transition_binding=_binding(), lease_ttl=0.05)
def push(idempotency_key: str) -> str:
    with side_effect():
        _provider("B")
        if CRASH_WINDOW:
            os._exit(19)
    return "push-1"


@ledger_sync(storage=LEDGER, transition_binding=_binding(), lease_ttl=0.05)
def track(idempotency_key: str, pushed: str) -> str:
    with side_effect():
        _provider("C")
    return pushed


def crash_after_b() -> None:
    if CRASH:
        os._exit(17)


register_composite_helper(crash_after_b)


@composite(
    LEDGER,
    operation_id_from=lambda _args, kwargs: kwargs["job_id"],
    lease_ttl=0.5,
)
def publish(job_id: str) -> str:
    create(idempotency_key=f"{job_id}:create")
    pushed = push(idempotency_key=f"{job_id}:push")
    crash_after_b()
    return track(idempotency_key=f"{job_id}:track", pushed=pushed)


if __name__ == "__main__":
    print(publish(job_id="job-1"))
