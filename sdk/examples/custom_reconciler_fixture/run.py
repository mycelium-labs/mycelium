"""A minimal, provider-neutral reconciler that passes the conformance kit.

Adapter authors can copy this file instead of starting from the Gmail
implementation. It talks to a fully local fake provider: no network, no
credentials, and no real provider SDK.

The kit checks fail-closed behaviour. Ambiguous evidence (zero matches,
duplicate matches, provider errors, malformed responses) must stay UNKNOWN.
It must never be reported as NOT_EXECUTED, which would let Mycelium re-run a
tool that may already have taken effect.

Run from the sdk directory::

    python examples/custom_reconciler_fixture/run.py
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass
from typing import Any

from mycelium.provider_conformance import (
    AdapterVerificationReport,
    ProviderCallAudit,
    ProviderObservation,
    adapter_report_is_verified,
    adapter_report_json,
    create_adapter_verification_report,
    run_provider_conformance_cases,
    verify_adapter_report_signature,
)
from mycelium.reconcile import ReconcileResult, ReconcileStatus

# Local handle shape. A real adapter would mirror the provider's id rules.
HANDLE_PATTERN = re.compile(r"^note_[a-z0-9]{6}$")


@dataclass(frozen=True)
class _Entry:
    """The one field the runner needs: the recorded provider reference."""

    external_operation_ref: str | None


class _ScriptedNoteStore:
    """Fake note provider. Records reads and traps every write."""

    def __init__(
        self,
        observations: tuple[ProviderObservation, ...],
        audit: ProviderCallAudit,
    ) -> None:
        self._observations = list(observations)
        self._audit = audit

    def search(self, handle: str) -> Any:
        self._audit.record_read("notes.search")
        if not self._observations:
            raise RuntimeError("conformance fixture exhausted scripted observations")
        observation = self._observations.pop(0)
        if observation.error is not None:
            raise observation.error
        if observation.malformed_response:
            return {"results": "not-a-list"}
        return {"results": [dict(match) for match in observation.matches]}

    def _forbidden(self, operation: str) -> Any:
        self._audit.record_write(operation)
        raise AssertionError(f"provider conformance forbids {operation}")

    def create(self, **kwargs: Any) -> Any:
        return self._forbidden("notes.create")

    def update(self, **kwargs: Any) -> Any:
        return self._forbidden("notes.update")

    def delete(self, **kwargs: Any) -> Any:
        return self._forbidden("notes.delete")


class LocalNoteReconciler:
    """Read-only reconciler. Every uncertain outcome stays UNKNOWN."""

    def __init__(self, store: _ScriptedNoteStore) -> None:
        self._store = store

    def reconcile(self, entry: _Entry) -> ReconcileResult:
        handle = entry.external_operation_ref
        # Reject a bad handle before any provider call.
        if not isinstance(handle, str) or HANDLE_PATTERN.fullmatch(handle) is None:
            return ReconcileResult.unknown()

        try:
            payload = self._store.search(handle)
        except Exception:
            # A provider error proves nothing about the effect.
            return ReconcileResult.unknown()

        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return ReconcileResult.unknown()
        if len(results) != 1:
            # Zero matches may be indexing lag; duplicates are ambiguous.
            return ReconcileResult.unknown()

        match = results[0]
        if not isinstance(match, dict) or not match.get("id"):
            return ReconcileResult.unknown()
        return ReconcileResult.completed({"note_id": str(match["id"])})


class LocalNoteConformanceFixture:
    """The ProviderConformanceFixture contract for the local fake provider."""

    adapter_name = "local-note-example"
    adapter_version = "1"
    valid_handle = "note_abc123"
    malformed_handles = (
        None,
        "",
        "   ",
        "note abc123",
        "note\tabc123",
        "note\nabc123",
        "note\x00abc123",
        "note_ABC123",
    )

    def build_reconciler(
        self,
        observations: tuple[ProviderObservation, ...],
        audit: ProviderCallAudit,
    ) -> LocalNoteReconciler:
        return LocalNoteReconciler(_ScriptedNoteStore(observations, audit))

    def make_entry(self, handle: Any) -> _Entry:
        return _Entry(external_operation_ref=handle)

    def source_bytes(self) -> bytes:
        # Bind a report to this reconciler, not to the example harness.
        return inspect.getsource(LocalNoteReconciler).encode("utf-8")


def main() -> None:
    fixture = LocalNoteConformanceFixture()

    print("=== custom reconciler conformance fixture ===\n")
    print(
        "This walks the provider-neutral case set against a local fake provider.\n"
        "A passing report is synthetic adapter evidence: it shows the reconciler\n"
        "fails closed for ambiguous evidence. It does not prove live provider\n"
        "permissions, consistency, or exactly-once execution.\n"
    )

    cases = run_provider_conformance_cases(fixture)
    print("Case results:")
    for case in cases:
        status = "PASS" if case.passed else "FAIL"
        print(f"  {status} {case.name:<36} expected={case.expected} actual={case.actual}")

    completed = sum(1 for case in cases if case.name == "exactly_one_match")
    assert completed == 1, "the one definite match must reconcile as COMPLETED"
    uncertain = [
        case
        for case in cases
        if case.name in {"zero_matches_fail_closed", "duplicate_matches_fail_closed"}
    ]
    assert all(case.actual == ReconcileStatus.UNKNOWN.value for case in uncertain), (
        "ambiguous provider evidence must stay UNKNOWN"
    )

    # A throwaway local key. Never commit a real signing key.
    signing_key = "local-example-signing-key"
    report = create_adapter_verification_report(
        fixture,
        signing_key=signing_key,
        signer_key_id="local-example-key",
        generated_at=0.0,
    )

    print("\nSigned report:")
    print(adapter_report_json(report))

    authentic = verify_adapter_report_signature(report, signing_key)
    accepted = adapter_report_is_verified(report, signing_key, fixture=fixture)
    tampered = report.to_dict()
    tampered["cases"][0]["actual"] = "NOT_EXECUTED"
    restored = AdapterVerificationReport.from_dict(tampered)
    tamper_refused = not verify_adapter_report_signature(restored, signing_key)

    print(f"report status={report.status}; authentic={authentic}; verified={accepted}")
    print(f"tampered report refused={tamper_refused}")
    assert authentic and accepted and tamper_refused

    print(
        "\nconformance ok: adapter=local-note-example cases=8 verified=True "
        "tamper_refused=True synthetic_adapter_evidence=True"
    )
    print(
        "\nThis report says nothing about a real provider. Treat it as adapter\n"
        "evidence for review, not as permission to run against production."
    )


if __name__ == "__main__":
    main()
