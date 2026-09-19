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
import time
import uuid
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


def test_release_completed_rejects_decisionless_effect_protocol(storage) -> None:
    ledger_inst = ActionLedger(storage=storage)
    with execution_scope(_scope()):
        claimed = ledger_inst.claim_side_effecting(
            "release-decisionless",
            "charge",
            (),
            {
                "request_id": "release-decisionless",
                "thread_id": "t1",
                "run_id": "r1",
            },
            _binding(),
        )
    ledger_inst.mark_unknown(
        claimed.request_id,
        error="worker disappeared",
        expected_fence=claimed.fence,
    )

    with pytest.raises(LedgerReleaseRefusedError, match="allowed durable ATTEMPTING"):
        ledger_inst.release(
            claimed.request_id,
            verified="completed",
            result={"charged": True},
            by="ops@example.com",
            reason="provider reports success",
        )

    stored = storage.get(claimed.request_id)
    assert stored is not None
    assert stored.resolved_terminal_outcome() != TerminalOutcome.COMPLETED


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
    ledger_inst.release("req-one-shot", verified="not_executed", by="ops", reason="verified")
    with pytest.raises(LedgerAlreadyResolvedError):
        ledger_inst.release("req-one-shot", verified="not_executed", by="ops", reason="again")
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
        ledger_inst.release("req-held", verified="not_executed", by="ops", reason="verified")

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
            kwargs={"amount": 5.0, "idempotency_key": "key-1"},
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

    assert [e.request_id for e in ledger_inst.list_transitions(stuck=True, tool="charge")] == [
        "stuck-blocked",
        "stuck-unknown",
    ]

    assert [
        e.request_id for e in ledger_inst.list_transitions(outcome=TerminalOutcome.BLOCKED)
    ] == ["stuck-blocked"]

    # A larger in-flight threshold hides the old in-flight entry again.
    stuck_wide = ledger_inst.list_transitions(stuck=True, in_flight_stuck_after=99999)
    assert {e.request_id for e in stuck_wide} == {"stuck-blocked", "stuck-unknown"}


def test_release_emits_audit_receipt_when_emitter_configured() -> None:
    from mycelium import AuditReceiptEmitter, InMemoryAuditReceiptStorage, verify_receipt

    receipt_storage = InMemoryAuditReceiptStorage()
    emitter = AuditReceiptEmitter(agent_id="demo", signing_key="test-key", storage=receipt_storage)
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


def test_static_operator_token_authorizes_the_correct_operator() -> None:
    from mycelium import StaticTokenOperatorAuthorizer

    storage = InMemoryLedgerStorage()
    storage.set(
        LedgerEntry(
            request_id="req-authorized-release",
            tool="charge",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.BLOCKED.value,
        )
    )
    ledger_inst = ActionLedger(
        storage=storage,
        operator_authorizer=StaticTokenOperatorAuthorizer({"alice@example.com": "alice-secret"}),
    )

    entry = ledger_inst.release(
        "req-authorized-release",
        verified="not_executed",
        by="alice@example.com",
        reason="provider confirms no charge",
        credential="alice-secret",
    )

    assert entry.resolved_by == "alice@example.com"


@pytest.mark.parametrize(
    ("operator_id", "credential"),
    [
        ("alice@example.com", None),
        ("alice@example.com", "wrong-secret"),
        ("mallory@example.com", "alice-secret"),
    ],
)
def test_static_operator_token_rejects_missing_wrong_or_mismatched_credentials(
    operator_id: str, credential: str | None
) -> None:
    from mycelium import StaticTokenOperatorAuthorizer

    storage = InMemoryLedgerStorage()
    storage.set(
        LedgerEntry(
            request_id="req-refused-release",
            tool="charge",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.BLOCKED.value,
        )
    )
    ledger_inst = ActionLedger(
        storage=storage,
        operator_authorizer=StaticTokenOperatorAuthorizer({"alice@example.com": "alice-secret"}),
    )

    with pytest.raises(LedgerReleaseRefusedError, match="not authorized"):
        ledger_inst.release(
            "req-refused-release",
            verified="not_executed",
            by=operator_id,
            reason="provider confirms no charge",
            credential=credential,
        )

    assert storage.get("req-refused-release").operator_resolution is None


def test_operator_authorizer_exception_fails_closed() -> None:
    class BrokenAuthorizer:
        def authorize_release(self, request, *, credential):
            raise RuntimeError("identity provider unavailable")

    storage = InMemoryLedgerStorage()
    storage.set(
        LedgerEntry(
            request_id="req-auth-error",
            tool="charge",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.BLOCKED.value,
        )
    )
    ledger_inst = ActionLedger(storage=storage, operator_authorizer=BrokenAuthorizer())

    with pytest.raises(LedgerReleaseRefusedError, match="failed closed"):
        ledger_inst.release(
            "req-auth-error",
            verified="not_executed",
            by="alice@example.com",
            reason="provider confirms no charge",
            credential="secret",
        )

    assert storage.get("req-auth-error").operator_resolution is None


def _signed_release_fixture(tmp_path: Path, *, clock=None):
    from mycelium import (
        InMemoryAtomicStateBackend,
        OperatorReleaseRequest,
        SignedOperatorReleaseCapabilityAuthorizer,
    )

    backend = InMemoryAtomicStateBackend()
    authorizer = SignedOperatorReleaseCapabilityAuthorizer(
        {"primary": "secret"},
        issuer="mycelium",
        audience="ops-api",
        nonce_backend=backend,
        clock=clock or time.time,
    )
    request = OperatorReleaseRequest(
        "alice", "req-cap", "charge", "not_executed", "effect-cap", "tenant-a", "policy-1"
    )
    token = authorizer.mint_capability(request, key_id="primary", expires_at=time.time() + 60)
    return authorizer, request, token, backend


def test_signed_capability_release_binds_authoritative_entry_and_is_one_shot(
    tmp_path: Path,
) -> None:

    storage = InMemoryLedgerStorage()
    storage.set(
        LedgerEntry(
            request_id="req-cap",
            tool="charge",
            args=[],
            kwargs={},
            status="failed",
            terminal_outcome=TerminalOutcome.BLOCKED.value,
            effect_id="effect-cap",
            tenant_id="tenant-a",
            policy_version="policy-1",
        )
    )
    authorizer, request, token, backend = _signed_release_fixture(tmp_path)
    ledger_inst = ActionLedger(storage=storage, operator_authorizer=authorizer)
    entry = ledger_inst.release(
        "req-cap",
        verified="not_executed",
        by="alice",
        reason="provider confirmed no charge",
        credential=token,
    )
    assert entry.operator_resolution == "not_executed"
    assert not authorizer.authorize_release(request, credential=token)
    assert backend.get("operator_release_capability_nonce", request.request_id) is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("operator_id", "mallory"),
        ("request_id", "other"),
        ("effect_id", "other-effect"),
        ("tool", "refund"),
        ("tenant", "tenant-b"),
        ("verified", "completed"),
        ("policy_version", "policy-2"),
    ],
)
def test_signed_capability_rejects_every_scope_mismatch(
    field: str, value: str, tmp_path: Path
) -> None:
    from dataclasses import replace

    authorizer, request, token, _ = _signed_release_fixture(tmp_path)
    assert not authorizer.authorize_release(replace(request, **{field: value}), credential=token)


def test_signed_capability_rejects_time_signature_identity_format_and_use_mutations(
    tmp_path: Path,
) -> None:
    import base64
    import hashlib
    import hmac

    from mycelium import (
        InMemoryAtomicStateBackend,
        OperatorReleaseRequest,
        SignedOperatorReleaseCapabilityAuthorizer,
    )

    now = [100.0]
    backend = InMemoryAtomicStateBackend()
    auth = SignedOperatorReleaseCapabilityAuthorizer(
        {"k": "secret"}, issuer="iss", audience="aud", nonce_backend=backend, clock=lambda: now[0]
    )
    request = OperatorReleaseRequest("op", "req", "tool", "not_executed", "eff", "ten", "pol")
    token = auth.mint_capability(request, key_id="k", expires_at=110, not_before=105)
    assert not auth.authorize_release(request, credential=token)
    now[0] = 106
    assert auth.authorize_release(request, credential=token)
    assert not auth.authorize_release(request, credential=token)
    for malformed in (None, "", "mcap1.bad", "mcap1.bad.bad", "other.x.y"):
        assert not auth.authorize_release(request, credential=malformed)

    parts = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    for key, value in (
        ("alg", "none"),
        ("max_uses", 2),
        ("token_version", 2),
        ("schema_version", 2),
        ("iss", "other"),
        ("aud", "other"),
        ("kid", "missing"),
    ):
        claims[key] = value
        encoded = (
            base64.urlsafe_b64encode(
                json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
            )
            .decode()
            .rstrip("=")
        )
        signature = hmac.new(b"secret", f"mcap1.{encoded}".encode(), hashlib.sha256).hexdigest()
        tampered = f"mcap1.{encoded}.{signature}"
        assert not auth.authorize_release(request, credential=tampered)
        claims[key] = {
            "alg": "HS256",
            "max_uses": 1,
            "token_version": 1,
            "schema_version": 1,
            "iss": "iss",
            "aud": "aud",
            "kid": "k",
        }[key]


def test_signed_capability_nonce_is_durable_and_concurrently_single_use(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from mycelium import (
        FileAtomicStateBackend,
        OperatorReleaseRequest,
        SignedOperatorReleaseCapabilityAuthorizer,
    )

    path = tmp_path / "capability-nonces.json"
    request = OperatorReleaseRequest("op", "req", "tool", "not_executed", "eff", "ten", "pol")
    first = SignedOperatorReleaseCapabilityAuthorizer(
        {"k": "secret"},
        issuer="iss",
        audience="aud",
        nonce_backend=FileAtomicStateBackend(path),
    )
    token = first.mint_capability(request, key_id="k", expires_at=time.time() + 60)
    checkers = [
        SignedOperatorReleaseCapabilityAuthorizer(
            {"k": "secret"},
            issuer="iss",
            audience="aud",
            nonce_backend=FileAtomicStateBackend(path),
        )
        for _ in range(8)
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(lambda checker: checker.authorize_release(request, credential=token), checkers)
        )
    assert sum(results) == 1
    restarted = SignedOperatorReleaseCapabilityAuthorizer(
        {"k": "secret"},
        issuer="iss",
        audience="aud",
        nonce_backend=FileAtomicStateBackend(path),
    )
    assert not restarted.authorize_release(request, credential=token)


def test_postgres_release_not_executed_round_trip() -> None:
    from backend_gates import require_postgres_dsn_or_skip

    from mycelium import PostgresLedgerStorage

    dsn = require_postgres_dsn_or_skip()
    storage = PostgresLedgerStorage(dsn, table="mycelium_test_action_ledger")
    request_id = f"pg-release-round-trip-{uuid.uuid4().hex}"
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
    entry = ledger_inst.release(request_id, verified="not_executed", by="ops", reason="verified")
    assert entry.operator_resolution == "not_executed"
    reloaded = storage.get(request_id)
    assert reloaded is not None
    assert reloaded.operator_resolution == "not_executed"
    assert reloaded.resolved_by == "ops"


def _seed_file_ledger(path: Path) -> str:
    storage = FileLedgerStorage(path)
    ledger_inst = ActionLedger(storage=storage)
    claimed = ledger_inst.claim("req-cli", "send_payment", (), {"amount": 10})
    ledger_inst.attach_external_operation_ref("req-cli", "pi_cli_1", expected_fence=claimed.fence)
    ledger_inst.mark_blocked(
        "req-cli", error="stale lease; maybe crossed", expected_fence=claimed.fence
    )
    return "req-cli"


def _seed_sqlite_ledger(path: Path) -> str:
    storage = SqliteLedgerStorage(path)
    ledger_inst = ActionLedger(storage=storage)
    claimed = ledger_inst.claim("req-cli-sqlite", "send_payment", (), {"amount": 10})
    ledger_inst.attach_external_operation_ref(
        "req-cli-sqlite", "pi_cli_sqlite", expected_fence=claimed.fence
    )
    ledger_inst.mark_blocked(
        "req-cli-sqlite",
        error="stale lease; maybe crossed",
        expected_fence=claimed.fence,
    )
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
