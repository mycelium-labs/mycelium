"""Run a credential-free composite_items interruption and recovery walkthrough."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory

from mycelium import (
    CompositeDefinitionDriftError,
    SideEffectClass,
    SqliteLedgerStorage,
    ToolTransitionBinding,
    TransitionScope,
    composite,
    composite_items,
    execution_scope,
    ledger_sync,
    register_composite_helper,
    side_effect,
)

ITEMS = [{"id": "A", "value": "alpha"}, {"id": "B", "value": "beta"}, {"id": "C", "value": "gamma"}]


class PlannedInterruption(RuntimeError):
    """The host deliberately stops before the last child is attempted."""


def run_phase(state_dir: Path, run_id: str, mode: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    provider_path = state_dir / "provider.sqlite"
    storage = SqliteLedgerStorage(state_dir / "ledger.sqlite")
    with sqlite3.connect(provider_path) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS effects "
            "(receipt INTEGER PRIMARY KEY, run_id TEXT, item_id TEXT, value TEXT)"
        )

    binding = ToolTransitionBinding.for_tool(
        agent_id="composite-items-example",
        policy_version="1",
        side_effect_class=SideEffectClass.KEYED_MUTATE,
        provider_idempotency_key_param="idempotency_key",
    )

    @ledger_sync(storage=storage, transition_binding=binding)
    def publish(idempotency_key: str, item: dict[str, str]) -> dict[str, str | int]:
        # This fake provider deliberately does not deduplicate requests. Counting
        # rows therefore shows whether a completed child body was run again.
        with side_effect(), sqlite3.connect(provider_path) as connection:
            cursor = connection.execute(
                "INSERT INTO effects(run_id, item_id, value) VALUES (?, ?, ?)",
                (run_id, item["id"], item["value"]),
            )
            receipt = {"id": item["id"], "value": item["value"], "receipt": cursor.lastrowid}
        print(f"published {item['id']}")
        return receipt

    def interrupt_before_last(item: dict[str, str]) -> None:
        if mode == "interrupt" and item["id"] == "C":
            raise PlannedInterruption("stopped before C; A and B are durably completed")

    register_composite_helper(interrupt_before_last)

    @composite(storage, operation_id_from=lambda _args, kwargs: kwargs["run_id"])
    def publish_batch(run_id: str, items: list[dict[str, str]]) -> dict[str, str | int]:
        for item in composite_items(items, max_items=3):
            interrupt_before_last(item)
            result = publish(idempotency_key=f"{run_id}:{item['id']}", item=item)
        return result

    items = [dict(item) for item in ITEMS]
    if mode == "change-value":
        items[0]["value"] = "changed"
    elif mode == "change-order":
        items.reverse()

    print(f"mode: {mode}; run_id: {run_id}")
    with sqlite3.connect(provider_path) as connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM effects WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
    try:
        with execution_scope(
            TransitionScope(thread_id="items-example", run_id=run_id, node="batch")
        ):
            result = publish_batch(run_id=run_id, items=items)
    except PlannedInterruption as exc:
        print(f"interrupted: {exc}")
    except CompositeDefinitionDriftError:
        if mode not in {"change-value", "change-order"}:
            raise
        print("rejected: item values/order differ from the pinned manifest")
    else:
        print(f"completed: {json.dumps(result, sort_keys=True)}")

    with sqlite3.connect(provider_path) as connection:
        effects = connection.execute(
            "SELECT item_id FROM effects WHERE run_id = ? ORDER BY receipt", (run_id,)
        ).fetchall()
    print(f"new provider effects: {len(effects) - before}")
    print(f"provider effects: {[row[0] for row in effects]}")
    return len(effects) - before, [str(row[0]) for row in effects]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("demo", "interrupt", "resume", "change-value", "change-order"),
        default="demo",
    )
    parser.add_argument("--state-dir", type=Path, default=Path(".mycelium-composite-items"))
    parser.add_argument("--run-id", default="items-demo")
    args = parser.parse_args()
    if args.mode == "demo":
        with TemporaryDirectory(prefix="mycelium-items-") as temporary:
            effects: list[str] = []
            last_new = 0
            for mode in ("interrupt", "change-value", "change-order", "resume", "resume"):
                last_new, effects = run_phase(Path(temporary), args.run_id, mode)
        print(f"\nwalkthrough ok: effects={effects} replay_effects={last_new}")
    else:
        run_phase(args.state_dir, args.run_id, args.mode)


if __name__ == "__main__":
    main()
