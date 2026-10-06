"""Real-process composite proof worker; never contacts a real business provider.

The SQLite provider is separate from the ledger and intentionally does NOT
deduplicate effects, so a repeated tool execution cannot hide behind a key.
Gates let the parent SIGKILL/SIGSTOP a worker at a known durable boundary.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from mycelium import (
    PostgresLedgerStorage,
    ReconcileResult,
    RedisLedgerStorage,
    SideEffectClass,
    SqliteLedgerStorage,
    ToolTransitionBinding,
    composite,
    composite_choice,
    composite_items,
    ledger_sync,
    record_external_operation,
    register_composite_helper,
    side_effect,
)
from mycelium.composite import _ControlStore
from mycelium.storage._helpers import redact_secrets

OPTIONS = json.loads(sys.argv[1])
ROOT = Path(OPTIONS["root"])
PROVIDER = ROOT / "provider.sqlite"
TTL = 0.6


def open_storage():
    backend = OPTIONS["backend"]
    if backend == "redis":
        return RedisLedgerStorage(
            os.environ["MYCELIUM_TEST_REDIS_URL"], prefix=f"{OPTIONS['resource']}:"
        )
    if backend == "postgres":
        return PostgresLedgerStorage(
            os.environ["MYCELIUM_TEST_POSTGRES_DSN"], table=OPTIONS["resource"], pool_min_size=0
        )
    return SqliteLedgerStorage(ROOT / "ledger.sqlite")


def checkpoint(window: str, stage: str = "any") -> None:
    if OPTIONS.get("pause") != window or OPTIONS.get("stage", "any") not in ("any", stage):
        return
    marker = ROOT / f"{OPTIONS['worker']}.ready"
    # The parent never reads until the complete marker has been renamed.
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(json.dumps({"window": window, "stage": stage}), encoding="utf-8")
    temporary.replace(marker)
    deadline = time.monotonic() + 20
    while not (ROOT / f"{OPTIONS['worker']}.continue").exists():
        if time.monotonic() > deadline:
            raise TimeoutError("proof gate was not released")
        time.sleep(0.01)


register_composite_helper(checkpoint)


def provider_effect(stage: str, key: str, value: str) -> dict:
    with sqlite3.connect(PROVIDER, timeout=10) as connection:
        connection.execute("PRAGMA synchronous=FULL")
        cursor = connection.execute(
            "INSERT INTO effects(stage, operation, value) VALUES (?, ?, ?)", (stage, key, value)
        )
        receipt = {"stage": stage, "value": value, "receipt": cursor.lastrowid}
        connection.execute(
            "UPDATE effects SET receipt=? WHERE id=?", (json.dumps(receipt), cursor.lastrowid)
        )
    return receipt


class ProviderLookup:
    def reconcile(self, entry):
        if OPTIONS.get("evidence", "completed") == "unknown":
            return ReconcileResult.unknown()
        # No writes and no provider calls with effects during reconciliation.
        with sqlite3.connect(f"file:{PROVIDER}?mode=ro", uri=True) as connection:
            rows = connection.execute(
                "SELECT receipt FROM effects WHERE operation=? ORDER BY id",
                (entry.external_operation_ref,),
            ).fetchall()
        if len(rows) == 1:
            return ReconcileResult.completed(json.loads(rows[0][0]))
        if not rows and OPTIONS.get("evidence") == "proven_absent":
            # Only this synchronous fake provider offers authoritative absence.
            return ReconcileResult.not_executed()
        return ReconcileResult.unknown()


STORAGE = open_storage()
BINDING = ToolTransitionBinding.for_tool(
    agent_id="composite-process-proof",
    policy_version="1",
    side_effect_class=SideEffectClass.KEYED_MUTATE,
    provider_idempotency_key_param="idempotency_key",
)


@ledger_sync(
    storage=STORAGE,
    transition_binding=BINDING,
    reconciler=ProviderLookup(),
    lease_ttl=TTL,
    poll_interval=0.01,
    poll_timeout=1,
)
def effect(idempotency_key: str, stage: str, value: str = "original") -> dict:
    if OPTIONS.get("record_ref", True):
        record_external_operation(idempotency_key)
    checkpoint("before_effect", stage)
    with side_effect():
        checkpoint("after_boundary", stage)
        receipt = provider_effect(stage, idempotency_key, value)
        checkpoint("after_effect", stage)
    return receipt


@ledger_sync(storage=STORAGE, transition_binding=BINDING, lease_ttl=TTL)
def decision(idempotency_key: str) -> bool:
    with side_effect():
        provider_effect("decision", idempotency_key, "true")
    return OPTIONS.get("decision", True)


DECORATOR = composite(
    STORAGE,
    namespace="process-proof",
    lease_ttl=TTL,
    definition=OPTIONS.get("definition"),
)


@DECORATOR
def sequence(operation_id: str, value: str) -> dict:
    effect(idempotency_key=f"{operation_id}:A", stage="A", value=value)
    checkpoint("after_child", "A")
    result = effect(idempotency_key=f"{operation_id}:B", stage="B")
    checkpoint("after_child", "B")
    return effect(idempotency_key=f"{operation_id}:C", stage="C", value=result["value"])


@DECORATOR
def fixed_loop(operation_id: str) -> dict:
    for index in range(3):
        result = effect(idempotency_key=f"{operation_id}:{index}", stage=f"item-{index}")
        checkpoint("after_child", f"item-{index}")
    return result


@DECORATOR
def shorter_loop(operation_id: str) -> dict:
    for index in range(2):
        result = effect(idempotency_key=f"{operation_id}:{index}", stage=f"item-{index}")
        checkpoint("after_child", f"item-{index}")
    return result


@DECORATOR
def reordered_sequence(operation_id: str) -> dict:
    effect(idempotency_key=f"{operation_id}:B", stage="B")
    effect(idempotency_key=f"{operation_id}:A", stage="A")
    return effect(idempotency_key=f"{operation_id}:C", stage="C")


@DECORATOR
def pinned_items(operation_id: str, items: list) -> dict:
    for item in composite_items(items, max_items=4):
        result = effect(idempotency_key=f"{operation_id}:{item}", stage=item)
        checkpoint("after_child", item)
    return result


@DECORATOR
def input_branch(operation_id: str, approved: bool) -> dict:
    if approved:
        result = effect(idempotency_key=f"{operation_id}:yes", stage="yes")
        checkpoint("after_child", "yes")
    else:
        result = effect(idempotency_key=f"{operation_id}:no", stage="no")
        checkpoint("after_child", "no")
    return effect(idempotency_key=f"{operation_id}:final", stage="final", value=result["stage"])


@DECORATOR
def result_branch(operation_id: str) -> dict:
    approved = decision(idempotency_key=f"{operation_id}:decision")
    if composite_choice(approved):
        result = effect(idempotency_key=f"{operation_id}:yes", stage="yes")
        checkpoint("after_child", "yes")
    else:
        result = effect(idempotency_key=f"{operation_id}:no", stage="no")
        checkpoint("after_child", "no")
    return effect(idempotency_key=f"{operation_id}:final", stage="final", value=result["stage"])


def instrument_control_store() -> None:
    # Exercise the existing protocol without adding production fault hooks.
    admit, resolve, finish = _ControlStore.admit, _ControlStore.resolve_child, _ControlStore.finish

    def admitted(self, *args, **kwargs):
        result = admit(self, *args, **kwargs)
        checkpoint("after_admission")
        return result

    def resolved(self, *args, **kwargs):
        checkpoint("before_child_resolution")
        return resolve(self, *args, **kwargs)

    def finishing(self, *args, **kwargs):
        checkpoint("before_parent_completion")
        return finish(self, *args, **kwargs)

    _ControlStore.admit = admitted
    _ControlStore.resolve_child = resolved
    _ControlStore.finish = finishing


if __name__ == "__main__":
    instrument_control_store()
    try:
        shape = OPTIONS.get("shape", "sequence")
        arguments = {"operation_id": "job-1"}
        if shape == "sequence":
            arguments["value"] = OPTIONS.get("value", "original")
        elif shape == "pinned_items":
            arguments["items"] = OPTIONS.get("items", ["item-0", "item-1", "item-2"])
        elif shape == "input_branch":
            arguments["approved"] = OPTIONS.get("approved", True)
        result = globals()[shape](**arguments)
        print(json.dumps({"status": "completed", "result": result}), flush=True)
    except Exception as exc:
        cause = exc
        while cause is not None:
            print(f"{type(cause).__name__}: {redact_secrets(str(cause))}", file=sys.stderr)
            cause = cause.__cause__
        print(json.dumps({"status": "blocked", "error": type(exc).__name__}), flush=True)
        sys.exit(2)
    finally:
        close = getattr(STORAGE, "close", None)
        if close is not None:
            close()
