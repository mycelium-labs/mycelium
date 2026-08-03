"""Tests for the operator release workflow (manual reconciliation).

A hard-blocked transition (``BLOCKED`` / ``UNKNOWN`` / ``FAILED_AFTER_EFFECT``
/ ``EXPIRED`` past the boundary) can be released by a recorded human
verification instead of staying blocked forever:

- ``release(verified="completed", result=...)`` marks the transition done;
  the next redispatch returns the recorded result without re-executing.
- ``release(verified="not_executed")`` stamps the entry; the next claim
  consumes the resolution and grants exactly one re-execution.

Release is one-shot, fail-closed, and never deletes ledger entries.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from mycelium import (
    ActionLedger,
    FileLedgerStorage,
    InMemoryLedgerStorage,
    LedgerAlreadyResolvedError,
    LedgerEntry,
    LedgerHardBlockError,
    LedgerReleaseRefusedError,
    RedisLedgerStorage,
    SideEffectBoundary,
    SideEffectClass,
    SqliteLedgerStorage,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    execution_scope,
    get_ledger,
    ledger,
    ledger_sync,
    record_external_operation,
    side_effect,
)


def _binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
    )


def _keyed_binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="1",
        side_effect_class=SideEffectClass.KEYED_MUTATE,
        provider_idempotency_key_param="idempotency_key",
    )


def _scope() -> TransitionScope:
    return TransitionScope(thread_id="t1", run_id="r1")


def _fake_redis(monkeypatch: pytest.MonkeyPatch):
    fakeredis = pytest.importorskip("fakeredis")
    fake = fakeredis.FakeRedis(decode_responses=True)

    def from_url(url: str, **kwargs: object) -> object:
        return fake

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", from_url)
    return fake


@pytest.fixture(params=["memory", "file", "sqlite", "redis"])
def storage(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    if request.param == "memory":
        return InMemoryLedgerStorage()
    if request.param == "file":
        return FileLedgerStorage(tmp_path / "ledger.json")
    if request.param == "sqlite":
        return SqliteLedgerStorage(tmp_path / "ledger.db")
    _fake_redis(monkeypatch)
    return RedisLedgerStorage("redis://test")


def _request_id(ledger_inst: ActionLedger, tool: str, kwargs: dict, binding) -> str:
    return ledger_inst.derive_request_id(tool, (), kwargs, transition_binding=binding)


def test_release_not_executed_grants_exactly_one_reexecution(storage) -> None:
    calls: list[float] = []
    fail_first = {"v": True}

    @ledger_sync(storage=storage, transition_binding=_binding())
    def charge(amount: float) -> dict[str, bool]:
        calls.append(amount)
        with side_effect():
            record_external_operation("pi_release_1")
            if fail_first["v"]:
                fail_first["v"] = False
                raise RuntimeError("provider timeout")
        return {"charged": True}

    ledger_inst = get_ledger(charge)
    assert ledger_inst is not None
    binding = _binding()

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            charge(amount=10.0, tool_call_id="c1")
        request_id = _request_id(
            ledger_inst, "charge", {"amount": 10.0, "tool_call_id": "c1"}, binding
        )

        # Hard-blocked until a human verifies with the provider.
        with pytest.raises(LedgerHardBlockError):
            charge(amount=10.0, tool_call_id="c1")

        entry = ledger_inst.release(
            request_id,
            verified="not_executed",
            by="ops@example.com",
            reason="provider shows no charge for pi_release_1",
        )
        assert entry.operator_resolution == "not_executed"
        assert entry.released_from_outcome == TerminalOutcome.UNKNOWN.value
        assert entry.resolved_by == "ops@example.com"

        # Next redispatch consumes the release and executes exactly once.
        result = charge(amount=10.0, tool_call_id="c1")
        assert result == {"charged": True}
        assert calls == [10.0, 10.0]

        # Subsequent dispatches RETURN the stored result; body does not run.
        again = charge(amount=10.0, tool_call_id="c1")
        assert again == {"charged": True}
        assert calls == [10.0, 10.0]

    final = storage.get(request_id)
    assert final is not None
    assert final.resolved_terminal_outcome() == TerminalOutcome.COMPLETED
    # Consumed (one-shot) but audit fields carried forward.
    assert final.operator_resolution is None
    assert final.resolved_by == "ops@example.com"
    assert final.released_from_outcome == TerminalOutcome.UNKNOWN.value


async def test_release_not_executed_async_claim_path(storage) -> None:
    calls: list[float] = []
    fail_first = {"v": True}

    @ledger(storage=storage, transition_binding=_binding())
    async def charge(amount: float) -> dict[str, bool]:
        calls.append(amount)
        with side_effect():
            record_external_operation("pi_release_async")
            if fail_first["v"]:
                fail_first["v"] = False
                raise RuntimeError("provider timeout")
        return {"charged": True}

    ledger_inst = get_ledger(charge)
    assert ledger_inst is not None

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            await charge(amount=3.0, tool_call_id="ca1")
        request_id = _request_id(
            ledger_inst, "charge", {"amount": 3.0, "tool_call_id": "ca1"}, _binding()
        )
        with pytest.raises(LedgerHardBlockError):
            await charge(amount=3.0, tool_call_id="ca1")

        ledger_inst.release(
            request_id,
            verified="not_executed",
            by="ops@example.com",
            reason="provider shows no charge",
        )
        result = await charge(amount=3.0, tool_call_id="ca1")
        assert result == {"charged": True}
        assert calls == [3.0, 3.0]

        again = await charge(amount=3.0, tool_call_id="ca1")
        assert again == {"charged": True}
        assert calls == [3.0, 3.0]


def test_release_completed_returns_result_without_reexecution(storage) -> None:
    calls: list[float] = []

    @ledger_sync(storage=storage, transition_binding=_binding())
    def charge(amount: float) -> dict[str, bool]:
        calls.append(amount)
        with side_effect():
            record_external_operation("pi_release_2")
            raise RuntimeError("provider timeout")

    ledger_inst = get_ledger(charge)
    assert ledger_inst is not None

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            charge(amount=7.0, tool_call_id="c2")
        request_id = _request_id(
            ledger_inst, "charge", {"amount": 7.0, "tool_call_id": "c2"}, _binding()
        )

        entry = ledger_inst.release(
            request_id,
            verified="completed",
            result={"charged": True, "via": "operator"},
            by="ops@example.com",
            reason="pi_release_2 succeeded at provider",
        )
        assert entry.resolved_terminal_outcome() == TerminalOutcome.COMPLETED
        assert entry.operator_resolution == "completed"
        assert entry.released_from_outcome == TerminalOutcome.UNKNOWN.value

        result = charge(amount=7.0, tool_call_id="c2")
        assert result == {"charged": True, "via": "operator"}
        assert calls == [7.0]  # body never re-ran


def test_release_is_one_shot(storage) -> None:
    ledger_inst = ActionLedger(storage=storage)
    storage.set(
        LedgerEntry(
            request_id="req-one-shot",
            tool="charge",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.BLOCKED.value,
        )
    )
    ledger_inst.release(
        "req-one-shot", verified="not_executed", by="ops", reason="verified"
    )
    with pytest.raises(LedgerAlreadyResolvedError):
        ledger_inst.release(
            "req-one-shot", verified="not_executed", by="ops", reason="again"
        )
    with pytest.raises(LedgerAlreadyResolvedError):
        ledger_inst.release(
            "req-one-shot", verified="completed", result={}, by="ops", reason="again"
        )


def test_release_refused_while_lease_held_allowed_once_expired(storage) -> None:
    ledger_inst = ActionLedger(storage=storage)
    storage.set(
        LedgerEntry(
            request_id="req-held",
            tool="charge",
            args=[],
            kwargs={},
            status="in-flight",
            terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
            lease_until=time.time() + 3600,
            side_effect_boundary=SideEffectBoundary.MAYBE_CROSSED.value,
        )
    )
    with pytest.raises(LedgerReleaseRefusedError, match="lease"):
        ledger_inst.release(
            "req-held", verified="not_executed", by="ops", reason="verified"
        )

    # Once the lease expires the transition is EXPIRED and releasable.
    current = storage.get("req-held")
    assert current is not None
    storage.set(
        LedgerEntry(
            request_id="req-held",
            tool="charge",
            args=[],
            kwargs={},
            status="in-flight",
            terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
            lease_until=time.time() - 1,
            side_effect_boundary=SideEffectBoundary.MAYBE_CROSSED.value,
        )
    )
    entry = ledger_inst.release(
        "req-held", verified="not_executed", by="ops", reason="worker is dead"
    )
    assert entry.operator_resolution == "not_executed"
    assert entry.released_from_outcome == TerminalOutcome.EXPIRED.value


def test_release_refused_on_completed_and_unknown_request(storage) -> None:
    ledger_inst = ActionLedger(storage=storage)
    storage.set(
        LedgerEntry(
            request_id="req-done",
            tool="charge",
            args=[],
            kwargs={},
            status="completed",
            terminal_outcome=TerminalOutcome.COMPLETED.value,
        )
    )
    with pytest.raises(LedgerReleaseRefusedError, match="COMPLETED"):
        ledger_inst.release("req-done", verified="not_executed", by="ops", reason="x")
    with pytest.raises(LedgerReleaseRefusedError, match="unknown"):
        ledger_inst.release("req-missing", verified="not_executed", by="ops", reason="x")


def test_release_validates_arguments(storage) -> None:
    ledger_inst = ActionLedger(storage=storage)
    storage.set(
        LedgerEntry(
            request_id="req-args",
            tool="charge",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.BLOCKED.value,
        )
    )
    with pytest.raises(LedgerReleaseRefusedError, match="verified"):
        ledger_inst.release("req-args", verified="unblock", by="ops", reason="x")
    with pytest.raises(LedgerReleaseRefusedError, match="by"):
        ledger_inst.release("req-args", verified="not_executed", by="", reason="x")
    with pytest.raises(LedgerReleaseRefusedError, match="reason"):
        ledger_inst.release("req-args", verified="not_executed", by="ops", reason="")


def test_keyed_mutate_still_enforces_provider_key_after_release(storage) -> None:
    """A not-executed release must not bypass provider idempotency key checks."""
    calls: list[dict] = []
    fail_first = {"v": True}
    binding = _keyed_binding()

    @ledger_sync(storage=storage, transition_binding=binding)
    def charge(amount: float, idempotency_key: str) -> dict[str, bool]:
        calls.append({"amount": amount, "key": idempotency_key})
        with side_effect():
            record_external_operation(idempotency_key)
            if fail_first["v"]:
                fail_first["v"] = False
                raise RuntimeError("provider timeout")
        return {"charged": True}

    ledger_inst = get_ledger(charge)
    assert ledger_inst is not None
    kwargs = {"amount": 5.0, "idempotency_key": "key-1", "tool_call_id": "ck1"}

    with execution_scope(_scope()):
        with pytest.raises(RuntimeError):
            charge(amount=5.0, idempotency_key="key-1", tool_call_id="ck1")
        request_id = _request_id(ledger_inst, "charge", kwargs, binding)

        # Without a release, a redispatch with a *different* key still blocks.
        with pytest.raises(LedgerHardBlockError):
            charge(amount=5.0, idempotency_key="key-2", tool_call_id="ck1")

        ledger_inst.release(
            request_id,
            verified="not_executed",
            by="ops",
            reason="provider never saw key-1",
        )

        # After the release the retry runs once; the fresh claim records the
        # incoming provider key so enforcement keeps working afterwards.
        result = charge(amount=5.0, idempotency_key="key-2", tool_call_id="ck1")
        assert result == {"charged": True}
        assert calls == [
            {"amount": 5.0, "key": "key-1"},
            {"amount": 5.0, "key": "key-2"},
        ]

    final = storage.get(request_id)
    assert final is not None
    assert final.provider_idempotency_key == "key-2"


def test_keyed_mutate_blocks_different_key_without_release(storage) -> None:
    binding = _keyed_binding()
    ledger_inst = ActionLedger(storage=storage)
    storage.set(
        LedgerEntry(
            request_id="req-keyed",
            tool="charge",
            args=[],
            kwargs={"amount": 5.0},
            status="failed",
            terminal_outcome=TerminalOutcome.FAILED_BEFORE_EFFECT.value,
            side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
            provider_idempotency_key="key-1",
        )
    )
    with pytest.raises(LedgerHardBlockError):
        ledger_inst.claim_side_effecting(
            "req-keyed",
            "charge",
            (),
            {"amount": 5.0, "idempotency_key": "key-2"},
            binding,
        )


def test_old_serialized_entries_deserialize_without_new_fields() -> None:
    old = {
        "request_id": "req-old",
        "tool": "charge",
        "args": [1],
        "kwargs": {"a": 2},
        "status": "failed",
        "terminal_outcome": "BLOCKED",
        "result": None,
        "error": "boom",
        "started_at": 1700000000.0,
        "finished_at": 1700000001.0,
        "lease_until": None,
        "owner": "host:1",
        "idempotency_key": "req-old",
        "receipt_ref": None,
        "side_effect_boundary": "not_crossed",
        "external_operation_ref": None,
        "provider_idempotency_key": None,
    }
    entry = LedgerEntry.from_dict(old)
    assert entry.operator_resolution is None
    assert entry.resolved_by is None
    assert entry.resolution_reason is None
    assert entry.resolved_at is None
    assert entry.released_from_outcome is None
    # Round-trip keeps the new keys present but null.
    serialized = entry.to_dict()
    assert serialized["operator_resolution"] is None
    assert LedgerEntry.from_dict(serialized) == entry


def test_list_transitions_filters(storage) -> None:
    ledger_inst = ActionLedger(storage=storage)
    now = time.time()
    storage.set(
        LedgerEntry(
            request_id="stuck-blocked",
            tool="charge",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.BLOCKED.value,
            started_at=now - 100,
        )
    )
    storage.set(
        LedgerEntry(
            request_id="stuck-unknown",
            tool="charge",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.UNKNOWN.value,
            started_at=now - 90,
        )
    )
    storage.set(
        LedgerEntry(
            request_id="ok-completed",
            tool="charge",
            args=[],
            kwargs={},
            status="completed",
            terminal_outcome=TerminalOutcome.COMPLETED.value,
            started_at=now - 80,
        )
    )
    storage.set(
        LedgerEntry(
            request_id="fresh-inflight",
            tool="search",
            args=[],
            kwargs={},
            status="in-flight",
            terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
            lease_until=None,  # unbounded: would never surface as EXPIRED
            started_at=now - 10,
        )
    )
    storage.set(
        LedgerEntry(
            request_id="old-inflight",
            tool="search",
            args=[],
            kwargs={},
            status="in-flight",
            terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
            lease_until=None,
            started_at=now - 7200,
        )
    )

    all_entries = ledger_inst.list_transitions()
    assert [e.request_id for e in all_entries] == [
        "old-inflight",
        "stuck-blocked",
        "stuck-unknown",
        "ok-completed",
        "fresh-inflight",
    ]  # oldest first

    stuck = ledger_inst.list_transitions(stuck=True)
    assert {e.request_id for e in stuck} == {
        "stuck-blocked",
        "stuck-unknown",
        "old-inflight",
    }

    assert [
        e.request_id
        for e in ledger_inst.list_transitions(stuck=True, tool="charge")
    ] == ["stuck-blocked", "stuck-unknown"]

    assert [
        e.request_id
        for e in ledger_inst.list_transitions(outcome=TerminalOutcome.BLOCKED)
    ] == ["stuck-blocked"]

    # A larger in-flight threshold hides the old in-flight entry again.
    stuck_wide = ledger_inst.list_transitions(stuck=True, in_flight_stuck_after=99999)
    assert {e.request_id for e in stuck_wide} == {"stuck-blocked", "stuck-unknown"}


def test_release_emits_audit_receipt_when_emitter_configured() -> None:
    from mycelium import AuditReceiptEmitter, InMemoryAuditReceiptStorage, verify_receipt

    receipt_storage = InMemoryAuditReceiptStorage()
    emitter = AuditReceiptEmitter(
        agent_id="demo", signing_key="test-key", storage=receipt_storage
    )
    storage = InMemoryLedgerStorage()
    ledger_inst = ActionLedger(storage=storage, audit_emitter=emitter)
    storage.set(
        LedgerEntry(
            request_id="req-receipt",
            tool="charge",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.BLOCKED.value,
        )
    )
    entry = ledger_inst.release(
        "req-receipt", verified="not_executed", by="ops", reason="verified offline"
    )
    receipts = receipt_storage.list_all()
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.request_id == "req-receipt"
    assert receipt.outputs["operator_release"] is True
    assert receipt.outputs["verified"] == "not_executed"
    assert receipt.outputs["resolved_by"] == "ops"
    assert verify_receipt(receipt, "test-key")
    assert entry.receipt_ref == receipt.receipt_id


@pytest.mark.skipif(
    not os.environ.get("MYCELIUM_TEST_POSTGRES_DSN"),
    reason="set MYCELIUM_TEST_POSTGRES_DSN to run Postgres integration tests",
)
def test_postgres_release_not_executed_round_trip() -> None:
    from mycelium import PostgresLedgerStorage

    dsn = os.environ["MYCELIUM_TEST_POSTGRES_DSN"]
    storage = PostgresLedgerStorage(dsn, table="mycelium_test_action_ledger")
    request_id = "pg-release-round-trip"
    storage.set(
        LedgerEntry(
            request_id=request_id,
            tool="charge",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.BLOCKED.value,
        )
    )
    ledger_inst = ActionLedger(storage=storage)
    entry = ledger_inst.release(
        request_id, verified="not_executed", by="ops", reason="verified"
    )
    assert entry.operator_resolution == "not_executed"
    reloaded = storage.get(request_id)
    assert reloaded is not None
    assert reloaded.operator_resolution == "not_executed"
    assert reloaded.resolved_by == "ops"


def _seed_file_ledger(path: Path) -> str:
    storage = FileLedgerStorage(path)
    ledger_inst = ActionLedger(storage=storage)
    ledger_inst.claim("req-cli", "send_payment", (), {"amount": 10})
    ledger_inst.attach_external_operation_ref("req-cli", "pi_cli_1")
    ledger_inst.mark_blocked("req-cli", error="stale lease; maybe crossed")
    return "req-cli"


def _seed_sqlite_ledger(path: Path) -> str:
    storage = SqliteLedgerStorage(path)
    ledger_inst = ActionLedger(storage=storage)
    ledger_inst.claim("req-cli-sqlite", "send_payment", (), {"amount": 10})
    ledger_inst.attach_external_operation_ref("req-cli-sqlite", "pi_cli_sqlite")
    ledger_inst.mark_blocked("req-cli-sqlite", error="stale lease; maybe crossed")
    return "req-cli-sqlite"


def test_cli_transitions_sqlite_backend_list_show(tmp_path: Path, capsys) -> None:
    from mycelium.__main__ import main

    db = tmp_path / "ledger.db"
    request_id = _seed_sqlite_ledger(db)

    assert main(["transitions", "list", "--sqlite", str(db), "--stuck"]) == 0
    out = capsys.readouterr().out
    assert request_id in out
    assert "BLOCKED" in out

    assert main(["transitions", "show", request_id, "--sqlite", str(db)]) == 0
    out = capsys.readouterr().out
    assert "external_operation_ref: pi_cli_sqlite" in out


def test_cli_transitions_file_backend_round_trip(tmp_path: Path, capsys) -> None:
    from mycelium.__main__ import main

    ledger_file = tmp_path / "ledger.json"
    request_id = _seed_file_ledger(ledger_file)

    assert main(["transitions", "list", "--file", str(ledger_file), "--stuck"]) == 0
    out = capsys.readouterr().out
    assert request_id in out
    assert "BLOCKED" in out
    assert "release" in out  # next-action hint

    assert main(["transitions", "list", "--file", str(ledger_file), "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["request_id"] for row in rows] == [request_id]
    assert rows[0]["resolved_outcome"] == "BLOCKED"

    assert main(["transitions", "show", request_id, "--file", str(ledger_file)]) == 0
    out = capsys.readouterr().out
    assert "send_payment" in out
    assert "external_operation_ref: pi_cli_1" in out
    assert "operator_resolution: -" in out

    assert (
        main(
            [
                "transitions",
                "release",
                request_id,
                "--file",
                str(ledger_file),
                "--verified",
                "not-executed",
                "--by",
                "ops@example.com",
                "--reason",
                "provider shows no charge",
            ]
        )
        == 0
    )
    capsys.readouterr()

    entry = FileLedgerStorage(ledger_file).get(request_id)
    assert entry is not None
    assert entry.operator_resolution == "not_executed"
    assert entry.resolved_by == "ops@example.com"
    assert entry.released_from_outcome == "BLOCKED"

    # One-shot via the CLI too.
    assert (
        main(
            [
                "transitions",
                "release",
                request_id,
                "--file",
                str(ledger_file),
                "--verified",
                "not-executed",
                "--by",
                "ops@example.com",
                "--reason",
                "again",
            ]
        )
        == 1
    )
    assert "one-shot" in capsys.readouterr().err

    # Unknown request id exits non-zero.
    assert (
        main(
            [
                "transitions",
                "release",
                "req-missing",
                "--file",
                str(ledger_file),
                "--verified",
                "completed",
                "--by",
                "ops",
                "--reason",
                "x",
            ]
        )
        == 2
    )


def test_cli_transitions_completed_with_result_json(tmp_path: Path, capsys) -> None:
    from mycelium.__main__ import main

    ledger_file = tmp_path / "ledger.json"
    request_id = _seed_file_ledger(ledger_file)

    assert (
        main(
            [
                "transitions",
                "release",
                request_id,
                "--file",
                str(ledger_file),
                "--verified",
                "completed",
                "--result-json",
                '{"charged": true}',
                "--by",
                "ops",
                "--reason",
                "provider shows the charge",
            ]
        )
        == 0
    )
    capsys.readouterr()
    entry = FileLedgerStorage(ledger_file).get(request_id)
    assert entry is not None
    assert entry.resolved_terminal_outcome() == TerminalOutcome.COMPLETED
    assert entry.result == {"charged": True}

    # --result-json with not-executed is a usage error.
    other = tmp_path / "ledger2.json"
    other_id = _seed_file_ledger(other)
    assert (
        main(
            [
                "transitions",
                "release",
                other_id,
                "--file",
                str(other),
                "--verified",
                "not-executed",
                "--result-json",
                "{}",
                "--by",
                "ops",
                "--reason",
                "x",
            ]
        )
        == 2
    )


def test_cli_transitions_config_and_memory_error(tmp_path: Path, capsys) -> None:
    from mycelium.__main__ import main

    ledger_file = tmp_path / "ledger.json"
    request_id = _seed_file_ledger(ledger_file)
    config = tmp_path / "mycelium.yaml"
    config.write_text(
        f"""
action_ledger:
  storage: file
  path: {ledger_file}
  tools: [send_payment]
tools:
  send_payment:
    ledger: true
""",
        encoding="utf-8",
    )
    assert main(["transitions", "list", "--config", str(config), "--stuck"]) == 0
    assert request_id in capsys.readouterr().out

    memory_config = tmp_path / "memory.yaml"
    memory_config.write_text(
        "action_ledger: {storage: memory, tools: [t]}\ntools: {t: {ledger: true}}\n",
        encoding="utf-8",
    )
    assert main(["transitions", "list", "--config", str(memory_config)]) == 2
    err = capsys.readouterr().err
    assert "memory" in err
    assert "Python API" in err
