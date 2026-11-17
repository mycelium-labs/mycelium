"""Shared helpers for the plain-Python SQLite crash walkthrough.

Private module (leading underscore): not a smoke-suite example on its own.
Ledger and fake-provider state live in separate SQLite files under one
temporary workspace so operator recovery never needs Redis, LangGraph,
credentials, or network access.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from mycelium import (
    ActionLedger,
    SideEffectClass,
    SqliteLedgerStorage,
    ToolTransitionBinding,
    ledger_sync,
    record_external_operation,
    side_effect,
)

ENV_WORKSPACE = "MYCELIUM_SQLITE_CRASH_WORKSPACE"
LEASE_TTL = 1.0
AGENT_ID = "sqlite-crash-demo"
POLICY_VERSION = "2026.08.1"
THREAD_ID = "sqlite-crash-thread"
RUN_ID = "sqlite-crash-run"
OPERATOR = "ops@example.test"


def require_workspace() -> Path:
    raw = os.environ.get(ENV_WORKSPACE)
    if not raw:
        raise SystemExit(
            f"set {ENV_WORKSPACE} to a temporary directory created for this walkthrough"
        )
    root = Path(raw).resolve()
    if not root.is_dir():
        raise SystemExit(f"workspace does not exist: {root}")
    return root


def ledger_path(root: Path) -> Path:
    return root / "ledger.sqlite"


def provider_path(root: Path) -> Path:
    return root / "provider.sqlite"


def action_id_path(root: Path) -> Path:
    return root / "action_id.txt"


def ready_path(root: Path) -> Path:
    return root / "worker.ready"


def err_path(root: Path) -> Path:
    return root / "worker.err"


def config_path(root: Path) -> Path:
    return root / "mycelium.yaml"


def read_action_id(root: Path) -> str:
    path = action_id_path(root)
    if not path.is_file():
        raise SystemExit(f"missing stable action id; run init first: {path}")
    return path.read_text(encoding="utf-8").strip()


def binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id=AGENT_ID,
        policy_version=POLICY_VERSION,
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
    )


def open_ledger(root: Path) -> ActionLedger:
    """Production-shaped ledger: death signal required; short lease for the demo."""
    return ActionLedger(
        storage=SqliteLedgerStorage(ledger_path(root)),
        reclaim_requires_death_signal=True,
        presumed_dead_after=3600.0,
    )


def init_provider(root: Path) -> None:
    path = provider_path(root)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS effects (
                id INTEGER PRIMARY KEY,
                operation TEXT NOT NULL,
                amount REAL NOT NULL,
                receipt TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.commit()


def provider_effect_count(root: Path) -> int:
    path = provider_path(root)
    if not path.is_file():
        return 0
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        row = conn.execute("SELECT COUNT(*) FROM effects").fetchone()
    return int(row[0]) if row else 0


def provider_lookup(root: Path, operation: str) -> dict[str, Any] | None:
    path = provider_path(root)
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        row = conn.execute(
            "SELECT receipt FROM effects WHERE operation=? ORDER BY id DESC LIMIT 1",
            (operation,),
        ).fetchone()
    if row is None:
        return None
    return json.loads(row[0])


def record_provider_effect(root: Path, operation: str, amount: float) -> dict[str, Any]:
    """Independent fake provider: durable local effect, no dedupe, no network."""
    path = provider_path(root)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA synchronous=FULL")
        cursor = conn.execute(
            "INSERT INTO effects(operation, amount, receipt, created_at) VALUES (?, ?, '', ?)",
            (operation, amount, time.time()),
        )
        receipt = {
            "charged": True,
            "amount": amount,
            "id": f"pi_local_{cursor.lastrowid}",
            "operation": operation,
        }
        conn.execute(
            "UPDATE effects SET receipt=? WHERE id=?",
            (json.dumps(receipt), cursor.lastrowid),
        )
        conn.commit()
    return receipt


def write_config(root: Path) -> None:
    """Optional YAML so the mycelium CLI can point at the same SQLite ledger."""
    config_path(root).write_text(
        f"""\
# Generated for this temporary workspace only. No credentials or network.
transition:
  agent_id: {AGENT_ID}
  policy_version: "{POLICY_VERSION}"
  lease_ttl: {LEASE_TTL}
  poll_interval: 0.05
  poll_timeout: 5
  reclaim_requires_death_signal: true
  presumed_dead_after: 3600

action_ledger:
  storage: sqlite
  path: {ledger_path(root).as_posix()!r}
  tools:
    - charge

tools:
  charge:
    side_effect_class: non_idempotent_mutate
""",
        encoding="utf-8",
    )


def make_charge(
    root: Path,
    *,
    hang_after_effect: bool = False,
    lease_ttl: float = LEASE_TTL,
    lease_renew_interval: float = 0.0,
):
    """Build a ledgered charge tool bound to this workspace."""
    storage = SqliteLedgerStorage(ledger_path(root))

    @ledger_sync(
        storage=storage,
        transition_binding=binding(),
        lease_ttl=lease_ttl,
        lease_renew_interval=lease_renew_interval,
        poll_interval=0.05,
        poll_timeout=5.0,
        reclaim_requires_death_signal=True,
        presumed_dead_after=3600.0,
    )
    def charge(amount: float) -> dict[str, Any]:
        action_id = read_action_id(root)
        op_ref = f"op:{action_id}"
        with side_effect():
            receipt = record_provider_effect(root, op_ref, amount)
            record_external_operation(op_ref)
            if hang_after_effect:
                ready_path(root).write_text("ready\n", encoding="utf-8")
                # Simulated crash window: effect recorded, ledger never completes.
                time.sleep(3600.0)
            return receipt

    return charge
