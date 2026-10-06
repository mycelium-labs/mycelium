"""Hard process death and competing-worker proofs, using real shared backends.

SQLite is always exercised. Redis/Postgres use the existing required-or-skip
gates; the primary CI job already supplies both services and requires them.
Provider receipts live in a separate FULL-synchronous SQLite database and
have no idempotency uniqueness constraint: duplicate effects are observable.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from backend_gates import require_postgres_dsn_or_skip, require_redis_or_skip

from mycelium import PostgresLedgerStorage, RedisLedgerStorage, SqliteLedgerStorage
from mycelium.composite import _ControlStore

SDK = Path(__file__).resolve().parents[1]
WORKER = SDK / "tests" / "fixtures" / "composite_shared_worker.py"
KEY = "process-proof:job-1"

pytestmark = pytest.mark.skipif(
    os.environ.get("MYCELIUM_TEST_COMPOSITE_PROCESS_PROOF") != "1",
    reason="opt-in process-kill proof; set MYCELIUM_TEST_COMPOSITE_PROCESS_PROOF=1",
)


@dataclass
class Proof:
    root: Path
    backend: str
    resource: str
    storage: object
    environment: dict[str, str]
    processes: list = field(default_factory=list)

    def start(self, worker="resume", **options):
        payload = {
            "root": str(self.root),
            "backend": self.backend,
            "resource": self.resource,
            "worker": worker,
            **options,
        }
        process = subprocess.Popen(
            [sys.executable, str(WORKER), json.dumps(payload)],
            cwd=SDK,
            env=self.environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        return process

    def ready(self, process, worker="crash"):
        deadline = time.monotonic() + 12
        while not (self.root / f"{worker}.ready").exists():
            if process.poll() is not None:
                output, errors = process.communicate()
                pytest.fail(f"worker exited before gate: {output}\n{errors}")
            assert time.monotonic() < deadline, "worker never reached the requested gate"
            time.sleep(0.01)

    def finish(self, process):
        output, errors = process.communicate(timeout=15)
        assert output.strip(), f"worker returned no structured result: {errors}"
        result = json.loads(output.strip().splitlines()[-1])
        assert process.returncode == (0 if result["status"] == "completed" else 2), errors
        return result

    def run(self, **options):
        return self.finish(self.start(**options))

    def kill(self, process):
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode < 0, "proof requires ungraceful OS process death"

    def wait_for_expiry(self):
        deadline = time.monotonic() + 5
        while True:
            record = _ControlStore(self.storage).load(KEY)
            assert record is not None
            if (record["lease_until"] or 0) <= time.time():
                return record
            assert time.monotonic() < deadline, "parent lease did not expire"
            time.sleep(0.01)

    def effects(self):
        with sqlite3.connect(self.root / "provider.sqlite") as connection:
            return connection.execute("SELECT stage, receipt FROM effects ORDER BY id").fetchall()

    def record(self):
        return _ControlStore(self.storage).load(KEY)


@pytest.fixture(params=["sqlite", "redis", "postgres"])
def proof(request, tmp_path):
    backend = request.param
    resource = f"cmp_proof_{uuid.uuid4().hex[:16]}"
    environment = dict(os.environ, PYTHONPATH=str(SDK))
    if backend == "redis":
        environment["MYCELIUM_TEST_REDIS_URL"] = require_redis_or_skip()
        storage = RedisLedgerStorage(environment["MYCELIUM_TEST_REDIS_URL"], prefix=f"{resource}:")
    elif backend == "postgres":
        environment["MYCELIUM_TEST_POSTGRES_DSN"] = require_postgres_dsn_or_skip()
        storage = PostgresLedgerStorage(
            environment["MYCELIUM_TEST_POSTGRES_DSN"], table=resource, pool_min_size=0
        )
    else:
        storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")
    with sqlite3.connect(tmp_path / "provider.sqlite") as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(
            "CREATE TABLE effects (id INTEGER PRIMARY KEY, stage TEXT, operation TEXT, "
            "value TEXT, receipt TEXT)"
        )
    state = Proof(tmp_path, backend, resource, storage, environment)
    try:
        yield state
    finally:
        for process in state.processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        close = getattr(storage, "close", None)
        if close is not None:
            close()
        # Remove only exact UUID-owned proof resources, never flush a database.
        if backend == "redis":
            import redis

            client = redis.Redis.from_url(environment["MYCELIUM_TEST_REDIS_URL"])
            keys = list(client.scan_iter(match=f"{resource}*"))
            if keys:
                client.delete(*keys)
            client.close()
        elif backend == "postgres":
            import psycopg
            from psycopg import sql

            with psycopg.connect(environment["MYCELIUM_TEST_POSTGRES_DSN"]) as connection:
                for table in (resource, f"{resource}_composites"):
                    connection.execute(
                        sql.SQL("DROP TABLE IF EXISTS {}").format(sql.Identifier(table))
                    )


@pytest.mark.parametrize(
    "window",
    [
        "after_admission",
        "before_effect",
        "before_child_resolution",
        "after_child",
        "before_parent_completion",
    ],
)
def test_killed_worker_replays_receipts_and_only_executes_remaining_children(proof, window):
    process = proof.start("crash", pause=window, stage="any")
    proof.ready(process)
    proof.kill(process)
    before = proof.wait_for_expiry()
    result = proof.run(evidence="proven_absent")
    assert result["status"] == "completed"
    assert [stage for stage, _ in proof.effects()] == ["A", "B", "C"]
    assert proof.run() == result  # Replay must return the very same receipt.
    assert len(proof.effects()) == 3
    record = proof.record()
    assert record["status"] == "COMPLETED"
    assert record["fence"] > before["fence"]
    assert record["replay_observed"] == record["replay_resolved"]


@pytest.mark.parametrize("window", ["after_boundary", "after_effect"])
def test_unknown_effect_blocks_progress_until_provider_evidence_resolves_it(proof, window):
    process = proof.start("crash", pause=window, stage="B")
    proof.ready(process)
    proof.kill(process)
    proof.wait_for_expiry()
    effects = proof.effects()
    blocked = proof.run(evidence="unknown")
    assert blocked == {"status": "blocked", "error": "LedgerHardBlockError"}
    assert proof.effects() == effects
    assert proof.record()["status"] != "COMPLETED"
    resolved = proof.run(evidence="proven_absent")
    assert resolved["status"] == "completed"
    assert [stage for stage, _ in proof.effects()] == ["A", "B", "C"]
    assert proof.run() == resolved
    assert len(proof.effects()) == 3


def test_missing_provider_reference_stays_blocked_even_if_lookup_is_available(proof):
    process = proof.start("crash", pause="after_effect", stage="B", record_ref=False)
    proof.ready(process)
    proof.kill(process)
    proof.wait_for_expiry()
    assert proof.run() == {"status": "blocked", "error": "LedgerHardBlockError"}
    assert [stage for stage, _ in proof.effects()] == ["A", "B"]


@pytest.mark.parametrize(
    ("shape", "stage", "expected"),
    [
        ("fixed_loop", "item-1", ["item-0", "item-1", "item-2"]),
        ("pinned_items", "item-1", ["item-0", "item-1", "item-2"]),
        ("input_branch", "yes", ["yes", "final"]),
        ("result_branch", "yes", ["decision", "yes", "final"]),
    ],
)
def test_pinned_loop_and_branch_paths_survive_process_death(proof, shape, stage, expected):
    process = proof.start("crash", shape=shape, pause="after_child", stage=stage)
    proof.ready(process)
    proof.kill(process)
    proof.wait_for_expiry()
    result = proof.run(shape=shape, decision=False)
    assert result["status"] == "completed"
    assert [stage for stage, _ in proof.effects()] == expected
    assert proof.run(shape=shape, decision=False) == result
    assert len(proof.effects()) == len(expected)


@pytest.mark.parametrize(
    ("shape", "changed"),
    [
        ("sequence", {"value": "changed"}),
        ("sequence", {"definition": "new-definition"}),
        ("pinned_items", {"items": ["item-2", "item-1", "item-0"]}),
        ("input_branch", {"approved": False}),
        ("sequence", {"shape": "reordered_sequence"}),
        ("fixed_loop", {"shape": "shorter_loop"}),
    ],
)
def test_changed_intent_or_pinned_definition_is_rejected_before_new_effects(proof, shape, changed):
    process = proof.start("crash", shape=shape, pause="after_child", definition="workflow-v1")
    proof.ready(process)
    proof.kill(process)
    proof.wait_for_expiry()
    before = proof.effects()
    result = proof.run(**{"shape": shape, "definition": "workflow-v1", **changed})
    assert result == {"status": "blocked", "error": "CompositeDefinitionDriftError"}
    assert proof.effects() == before


def test_concurrent_resumers_share_one_parent_authority(proof):
    process = proof.start("crash", pause="after_child", stage="A")
    proof.ready(process)
    proof.kill(process)
    proof.wait_for_expiry()
    winner = proof.start("winner", pause="after_child", stage="A")
    proof.ready(winner, "winner")
    assert proof.run(worker="contender") == {"status": "blocked", "error": "CompositeBusyError"}
    (proof.root / "winner.continue").touch()
    result = proof.finish(winner)
    assert result["status"] == "completed"
    assert proof.run() == result
    assert [stage for stage, _ in proof.effects()] == ["A", "B", "C"]


@pytest.mark.skipif(not hasattr(signal, "SIGSTOP"), reason="requires process suspension")
@pytest.mark.parametrize("window", ["after_child", "before_effect"])
def test_stale_worker_cannot_admit_or_send_after_parent_takeover(proof, window):
    process = proof.start("stale", pause=window, stage="A" if window == "after_child" else "B")
    proof.ready(process, "stale")
    os.kill(process.pid, signal.SIGSTOP)
    old = proof.wait_for_expiry()
    winner = proof.run(worker="winner", evidence="proven_absent")
    assert winner["status"] == "completed"
    assert proof.record()["fence"] > old["fence"]
    os.kill(process.pid, signal.SIGCONT)
    (proof.root / "stale.continue").touch()
    assert proof.finish(process) == {"status": "blocked", "error": "CompositeAuthorityError"}
    assert [stage for stage, _ in proof.effects()] == ["A", "B", "C"]
    assert proof.record()["status"] == "COMPLETED"
