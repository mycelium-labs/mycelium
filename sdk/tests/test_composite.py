"""Behavioral coverage for the durable straight-line composite protocol."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from mycelium import (
    CompositeAuthorityError,
    CompositeBusyError,
    CompositeDefinitionDriftError,
    CompositeUnsupportedError,
    SideEffectClass,
    SqliteLedgerStorage,
    ToolTransitionBinding,
    composite,
    ledger,
    ledger_sync,
    register_composite_helper,
    side_effect,
    side_effect_async,
)
from mycelium.composite import CompositeInvocation, _ControlStore


def _binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="composite-test",
        policy_version="1",
        side_effect_class=SideEffectClass.KEYED_MUTATE,
        provider_idempotency_key_param="idempotency_key",
    )


def test_sqlite_composite_replays_completed_children_and_runs_remaining_step(tmp_path) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")
    calls: list[str] = []
    crash = {"enabled": True}

    @ledger_sync(storage=storage, transition_binding=_binding())
    def create(idempotency_key: str) -> dict[str, str]:
        with side_effect():
            calls.append("A")
        return {"commit": "c1"}

    @ledger_sync(storage=storage, transition_binding=_binding())
    def push(idempotency_key: str) -> dict[str, str]:
        with side_effect():
            calls.append("B")
        return {"pushed": "c1"}

    @ledger_sync(storage=storage, transition_binding=_binding())
    def track(idempotency_key: str, pushed: dict[str, str]) -> dict[str, str]:
        with side_effect():
            calls.append("C")
        return {"tracked": pushed["pushed"]}

    def crash_after_b() -> None:
        if crash["enabled"]:
            raise RuntimeError("simulated process failure")

    register_composite_helper(crash_after_b)

    @composite(storage, operation_id_from=lambda _args, kwargs: kwargs["job_id"])
    def publish(job_id: str) -> dict[str, str]:
        create(idempotency_key="create-1")
        pushed = push(idempotency_key="push-1")
        crash_after_b()
        return track(idempotency_key="track-1", pushed=pushed)

    with pytest.raises(RuntimeError, match="simulated"):
        publish(job_id="job-1")
    assert calls == ["A", "B"]

    crash["enabled"] = False
    assert publish(job_id="job-1") == {"tracked": "c1"}
    assert calls == ["A", "B", "C"]

    controls = json.loads((tmp_path / "ledger.sqlite.composites.json").read_text())
    record = controls["mycelium:job-1"]
    assert record["status"] == "COMPLETED"
    assert len(record["manifest"]["steps"]) == 3
    assert record["manifest_digest"]


def test_composite_rejects_unsupported_control_flow(tmp_path) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")

    @ledger_sync(storage=storage, transition_binding=_binding())
    def effect(idempotency_key: str) -> str:
        return "ok"

    with pytest.raises(CompositeUnsupportedError, match="straight-line"):

        @composite(storage)
        def unsupported(operation_id: str) -> str:
            if operation_id:
                return effect(idempotency_key="x")
            return effect(idempotency_key="y")


def test_composite_rejects_expression_control_flow_before_any_effect(tmp_path) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")
    calls: list[str] = []

    @ledger_sync(storage=storage, transition_binding=_binding())
    def effect(idempotency_key: str) -> str:
        with side_effect():
            calls.append(idempotency_key)
        return "effect-result"

    enabled = True
    with pytest.raises(CompositeUnsupportedError, match="conditional expressions"):

        @composite(storage)
        def conditional(operation_id: str) -> str:
            return effect(idempotency_key="conditional") if enabled else "skipped"

    with pytest.raises(CompositeUnsupportedError, match="short-circuit"):

        @composite(storage)
        def short_circuit(operation_id: str) -> str:
            return enabled and effect(idempotency_key="short-circuit")

    with pytest.raises(CompositeUnsupportedError, match="comprehensions"):

        @composite(storage)
        def comprehension(operation_id: str) -> list[str]:
            return [effect(idempotency_key="comprehension") for _ in (0,)]

    with pytest.raises(CompositeUnsupportedError, match="nested or embedded calls"):

        @composite(storage)
        def nested(operation_id: str) -> str:
            return str(effect(idempotency_key="nested"))

    with pytest.raises(CompositeUnsupportedError, match="early returns"):

        @composite(storage)
        def early_return(operation_id: str) -> str:
            return "skipped"
            effect(idempotency_key="early")

    assert calls == []


def test_composite_finish_rejects_admission_without_resolution(tmp_path) -> None:
    from mycelium.composite import CompositeInvocation

    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")

    @ledger_sync(storage=storage, transition_binding=_binding())
    def effect(idempotency_key: str) -> str:
        return "ok"

    @composite(storage)
    def publish(operation_id: str) -> str:
        return effect(idempotency_key="guard")

    invocation = CompositeInvocation(
        storage,
        "admission-only",
        publish._mycelium_composite_manifest,
        namespace="mycelium",
        lease_ttl=10,
    )
    invocation.__enter__()
    binding = getattr(effect, "_mycelium_transition_binding")
    invocation.prepare_child("effect", (), {"idempotency_key": "guard"}, binding)
    with pytest.raises(CompositeDefinitionDriftError, match="without resolving"):
        invocation.__exit__(None, None, None)
    invocation.store.release(invocation.key, invocation.owner, invocation.fence)


def test_replays_all_completed_children_after_parent_completion_window(
    tmp_path, monkeypatch
) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")
    calls: list[str] = []

    @ledger_sync(storage=storage, transition_binding=_binding())
    def first(idempotency_key: str) -> str:
        with side_effect():
            calls.append("first")
        return "first"

    @ledger_sync(storage=storage, transition_binding=_binding())
    def second(idempotency_key: str) -> str:
        with side_effect():
            calls.append("second")
        return "second"

    @composite(storage)
    def publish(operation_id: str) -> str:
        first(idempotency_key="first")
        return second(idempotency_key="second")

    original_resolve = CompositeInvocation.resolve_child
    fail_once = True

    def fail_parent_finish_window(self, step_id: str) -> None:
        nonlocal fail_once
        original_resolve(self, step_id)
        if fail_once and step_id == self.manifest.steps[-1].step_id:
            fail_once = False
            raise RuntimeError("simulated parent completion crash")

    monkeypatch.setattr(CompositeInvocation, "resolve_child", fail_parent_finish_window)
    with pytest.raises(RuntimeError, match="parent completion crash"):
        publish(operation_id="parent-window")

    monkeypatch.setattr(CompositeInvocation, "resolve_child", original_resolve)
    assert publish(operation_id="parent-window") == "second"
    assert calls == ["first", "second"]


def test_composite_rejects_opaque_calls_instead_of_skipping_them(tmp_path) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")

    def uninstrumented_provider() -> str:
        return "external"

    with pytest.raises(CompositeUnsupportedError, match="unresolvable call"):

        @composite(storage)
        def unsupported(operation_id: str) -> str:
            return uninstrumented_provider()


def test_composite_definition_is_pinned(tmp_path) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")

    @ledger_sync(storage=storage, transition_binding=_binding())
    def effect(idempotency_key: str) -> str:
        with side_effect():
            return "ok"

    @composite(storage)
    def publish(operation_id: str) -> str:
        return effect(idempotency_key="x")

    assert publish(operation_id="job-1") == "ok"

    # A new decorator definition cannot silently reuse the old invocation.
    @composite(storage, definition="changed-definition")
    def changed(operation_id: str) -> str:
        return effect(idempotency_key="x")

    with pytest.raises(CompositeDefinitionDriftError):
        changed(operation_id="job-1")


def test_file_backend_survives_subprocess_termination_and_restart(tmp_path) -> None:
    sdk_path = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(sdk_path),
        "MYCELIUM_COMPOSITE_FIXTURE_DIR": str(tmp_path),
        "MYCELIUM_COMPOSITE_CRASH": "1",
    }
    first = subprocess.run(
        [sys.executable, "tests/fixtures/composite_worker.py"],
        cwd=sdk_path,
        env=env,
        check=False,
    )
    assert first.returncode == 17

    time.sleep(0.6)
    env["MYCELIUM_COMPOSITE_CRASH"] = "0"
    second = subprocess.run(
        [sys.executable, "tests/fixtures/composite_worker.py"],
        cwd=sdk_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert second.stdout.strip() == "push-1"
    assert json.loads((tmp_path / "provider-counts.json").read_text()) == {
        "A": 1,
        "B": 1,
        "C": 1,
    }


def test_provider_success_before_result_persistence_fails_closed(tmp_path) -> None:
    sdk_path = Path(__file__).resolve().parents[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(sdk_path),
        "MYCELIUM_COMPOSITE_FIXTURE_DIR": str(tmp_path),
        "MYCELIUM_COMPOSITE_CRASH": "0",
        "MYCELIUM_COMPOSITE_CRASH_WINDOW": "1",
    }
    first = subprocess.run(
        [sys.executable, "tests/fixtures/composite_worker.py"],
        cwd=sdk_path,
        env=env,
        check=False,
    )
    assert first.returncode == 19

    time.sleep(0.6)
    env["MYCELIUM_COMPOSITE_CRASH_WINDOW"] = "0"
    second = subprocess.run(
        [sys.executable, "tests/fixtures/composite_worker.py"],
        cwd=sdk_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert second.returncode != 0
    assert json.loads((tmp_path / "provider-counts.json").read_text()) == {
        "A": 1,
        "B": 1,
    }


def test_same_process_invocations_do_not_share_parent_authority(tmp_path) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")
    started = threading.Event()
    release = threading.Event()

    @ledger_sync(storage=storage, transition_binding=_binding())
    def slow(idempotency_key: str) -> str:
        started.set()
        release.wait(timeout=2)
        with side_effect():
            return "done"

    @composite(storage, lease_ttl=2)
    def publish(operation_id: str) -> str:
        return slow(idempotency_key="slow-1")

    worker = threading.Thread(target=lambda: publish(operation_id="same"))
    worker.start()
    assert started.wait(timeout=2)
    with pytest.raises(CompositeBusyError, match="live worker"):
        publish(operation_id="same")
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_parent_lease_renews_during_long_child(tmp_path) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")

    @ledger_sync(storage=storage, transition_binding=_binding())
    def slow(idempotency_key: str) -> str:
        time.sleep(0.16)
        with side_effect():
            return "done"

    @composite(storage, lease_ttl=0.05)
    def publish(operation_id: str) -> str:
        return slow(idempotency_key="slow-1")

    assert publish(operation_id="renewed") == "done"


def test_renewal_failure_blocks_the_next_child(tmp_path, monkeypatch) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")
    pause_started = threading.Event()
    renewal_failed = threading.Event()
    release_pause = threading.Event()
    calls: list[str] = []

    @ledger_sync(storage=storage, transition_binding=_binding())
    def first(idempotency_key: str) -> str:
        with side_effect():
            calls.append("first")
        return "first"

    @ledger_sync(storage=storage, transition_binding=_binding())
    def second(idempotency_key: str) -> str:
        with side_effect():
            calls.append("second")
        return "second"

    def pause() -> None:
        pause_started.set()
        release_pause.wait(timeout=2)

    register_composite_helper(pause)
    original_renew = _ControlStore.renew
    renewals = 0

    def fail_after_initial_renew(self, key, owner, fence, lease_ttl):
        nonlocal renewals
        renewals += 1
        if renewals >= 2:
            renewal_failed.set()
            raise CompositeAuthorityError("controlled renewal failure")
        return original_renew(self, key, owner, fence, lease_ttl)

    monkeypatch.setattr(_ControlStore, "renew", fail_after_initial_renew)

    @composite(storage, lease_ttl=0.2, renewal_interval=0.01)
    def publish(operation_id: str) -> str:
        first(idempotency_key="first")
        pause()
        return second(idempotency_key="second")

    errors: list[BaseException] = []

    def run() -> None:
        try:
            publish(operation_id="renewal-failure")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert pause_started.wait(timeout=2)
    assert renewal_failed.wait(timeout=2)
    release_pause.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert any(isinstance(error, CompositeAuthorityError) for error in errors)
    assert calls == ["first"]


def test_stale_worker_cannot_admit_after_parent_reclaim(tmp_path, monkeypatch) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")
    started = threading.Event()
    calls: list[str] = []

    @ledger_sync(storage=storage, transition_binding=_binding())
    def effect(idempotency_key: str) -> str:
        with side_effect():
            calls.append("effect")
        return "ok"

    def pause_for_reclaim() -> None:
        started.set()
        time.sleep(0.08)

    register_composite_helper(pause_for_reclaim)
    original_start_renewal = CompositeInvocation._start_renewal
    monkeypatch.setattr(CompositeInvocation, "_start_renewal", lambda _self: None)

    @composite(storage, lease_ttl=0.05)
    def publish(operation_id: str) -> str:
        pause_for_reclaim()
        return effect(idempotency_key="reclaimed")

    errors: list[BaseException] = []

    def stale_worker() -> None:
        try:
            publish(operation_id="reclaim")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=stale_worker)
    worker.start()
    assert started.wait(timeout=2)
    monkeypatch.setattr(CompositeInvocation, "_start_renewal", original_start_renewal)
    time.sleep(0.06)
    assert publish(operation_id="reclaim") == "ok"
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert any(isinstance(error, CompositeAuthorityError) for error in errors)
    assert calls == ["effect"]


def test_reclaimed_fence_rejects_stale_renew_and_finish(tmp_path) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")

    @ledger_sync(storage=storage, transition_binding=_binding())
    def effect(idempotency_key: str) -> str:
        return "ok"

    @composite(storage)
    def publish(operation_id: str) -> str:
        return effect(idempotency_key="fence")

    first = CompositeInvocation(
        storage,
        "stale-fence",
        publish._mycelium_composite_manifest,
        namespace="mycelium",
        lease_ttl=0.05,
    )
    time.sleep(0.06)
    second = CompositeInvocation(
        storage,
        "stale-fence",
        publish._mycelium_composite_manifest,
        namespace="mycelium",
        lease_ttl=1,
    )
    with pytest.raises(CompositeAuthorityError):
        first.renew()
    expected = tuple(step.step_id for step in publish._mycelium_composite_manifest.steps)
    with pytest.raises(CompositeAuthorityError):
        first.store.finish(first.key, first.owner, first.fence, expected, (), frozenset())
    second.store.release(second.key, second.owner, second.fence)


def test_child_result_must_be_faithfully_reconstructable(tmp_path) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")

    @ledger_sync(storage=storage, transition_binding=_binding())
    def tuple_result(idempotency_key: str) -> tuple[str, ...]:
        with side_effect():
            return ("not", "json-faithful")

    @composite(storage)
    def publish(operation_id: str) -> tuple[str, ...]:
        return tuple_result(idempotency_key="tuple-1")

    with pytest.raises(CompositeUnsupportedError, match="faithfully"):
        publish(operation_id="serialization")


def test_async_composite_uses_the_same_child_protocol(tmp_path) -> None:
    storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")

    @ledger(storage=storage, transition_binding=_binding())
    async def effect(idempotency_key: str) -> dict[str, str]:
        async with side_effect_async():
            return {"value": "ok"}

    @composite(storage)
    async def publish(operation_id: str) -> dict[str, str]:
        result = await effect(idempotency_key="async-1")
        return {"copied": result["value"]}

    assert asyncio.run(publish(operation_id="async")) == {"copied": "ok"}


def test_async_cancellation_releases_parent_without_leaking_progress(tmp_path) -> None:
    async def scenario() -> None:
        storage = SqliteLedgerStorage(tmp_path / "ledger.sqlite")
        started = asyncio.Event()
        release = asyncio.Event()

        @ledger(storage=storage, transition_binding=_binding())
        async def effect(idempotency_key: str) -> str:
            async with side_effect_async():
                return "ok"

        async def pause() -> None:
            started.set()
            await release.wait()

        register_composite_helper(pause)

        @composite(storage)
        async def publish(operation_id: str) -> str:
            await pause()
            return await effect(idempotency_key="cancelled")

        task = asyncio.create_task(publish(operation_id="cancelled"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        assert await publish(operation_id="cancelled") == "ok"

    asyncio.run(scenario())
