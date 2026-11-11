"""Synthetic outcome-telemetry and DTTR walkthrough (#328).

Demonstrates the local data flow from append-only outcome emission
(``OutcomeEmitter`` + ``FileOutcomeStorage``) to a reloaded
``DttrReport`` (``compute_dttr_from_storage``), without external
credentials, network services, or extra analytics APIs.

Run from the sdk directory::

    python examples/outcome_telemetry_dttr.py

All transitions, request IDs, and operation references in this script are
synthetic. Duplicate Tool Transition Rate (DTTR) measures silent duplicate
tool-body executions divided by long-running or redispatched transitions;
it is not a count of prevented provider side effects.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mycelium import (
    FileLedgerStorage,
    FileOutcomeStorage,
    OutcomeEmitter,
    SideEffectBoundary,
    SideEffectClass,
    TerminalOutcome,
    ToolTransitionBinding,
    TransitionScope,
    compute_dttr_from_storage,
    execution_scope,
    ledger_sync,
)
from mycelium.outcome_emit import (
    EVENT_BODY_COMPLETE,
    EVENT_BODY_START,
    EVENT_RELEASE,
    EVENT_RESOLUTION,
    GATE_ALLOW,
    GATE_HARD_BLOCK,
    GATE_RELEASE,
)


def emit_synthetic_outcomes(outcome_path: Path, ledger_path: Path) -> None:
    """Append a synthetic sequence to a temporary NDJSON FileOutcomeStorage."""
    storage = FileOutcomeStorage(outcome_path)
    emitter = OutcomeEmitter(agent_id="synthetic-billing-agent", storage=storage)

    # Transition 1 (req-synthetic-ambiguous-001):
    # Starts executing, hits an unresolved provider timeout (HARD_BLOCK / UNKNOWN),
    # then receives an operator release verifying NOT_EXECUTED and completes on
    # the authorized retry.
    ambiguous_req = "req-synthetic-ambiguous-001"
    emitter.emit_event(
        tool="issue_refund",
        request_id=ambiguous_req,
        event=EVENT_RESOLUTION,
        gate=GATE_ALLOW,
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE.value,
        dispatch_attempt=1,
        run_id="synthetic-run-1",
        policy_version="2026.11.0",
        resolution_reason="initial_claim_allowed",
    )
    emitter.emit_event(
        tool="issue_refund",
        request_id=ambiguous_req,
        event=EVENT_BODY_START,
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE.value,
        tool_body_executed=True,
        dispatch_attempt=1,
        authorized_reexec=False,
        run_id="synthetic-run-1",
        policy_version="2026.11.0",
    )
    emitter.emit_event(
        tool="issue_refund",
        request_id=ambiguous_req,
        event=EVENT_RESOLUTION,
        gate=GATE_HARD_BLOCK,
        terminal_outcome=TerminalOutcome.UNKNOWN.value,
        side_effect_boundary=SideEffectBoundary.MAYBE_CROSSED.value,
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE.value,
        dispatch_attempt=2,
        run_id="synthetic-run-1",
        policy_version="2026.11.0",
        resolution_reason="synthetic_timeout_unresolved",
    )
    emitter.emit_event(
        tool="issue_refund",
        request_id=ambiguous_req,
        event=EVENT_RELEASE,
        gate=GATE_RELEASE,
        terminal_outcome=TerminalOutcome.FAILED_BEFORE_EFFECT.value,
        side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE.value,
        run_id="synthetic-run-1",
        policy_version="2026.11.0",
        resolution_reason="synthetic_operator_verified_not_executed",
    )
    emitter.emit_event(
        tool="issue_refund",
        request_id=ambiguous_req,
        event=EVENT_RESOLUTION,
        gate=GATE_ALLOW,
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE.value,
        dispatch_attempt=3,
        run_id="synthetic-run-1",
        policy_version="2026.11.0",
        resolution_reason="authorized_retry_after_not_executed",
    )
    emitter.emit_event(
        tool="issue_refund",
        request_id=ambiguous_req,
        event=EVENT_BODY_START,
        terminal_outcome=TerminalOutcome.IN_FLIGHT.value,
        side_effect_boundary=SideEffectBoundary.NOT_CROSSED.value,
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE.value,
        tool_body_executed=True,
        dispatch_attempt=3,
        authorized_reexec=True,
        run_id="synthetic-run-1",
        policy_version="2026.11.0",
    )
    emitter.emit_event(
        tool="issue_refund",
        request_id=ambiguous_req,
        event=EVENT_BODY_COMPLETE,
        terminal_outcome=TerminalOutcome.COMPLETED.value,
        side_effect_boundary=SideEffectBoundary.CROSSED.value,
        side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE.value,
        dispatch_attempt=3,
        run_id="synthetic-run-1",
        policy_version="2026.11.0",
        external_operation_ref="synthetic-refund-ref-001",
    )

    # Transition 2 (req-synthetic-completed-002):
    # A clean single-dispatch completed action emitted via @ledger_sync.
    binding = ToolTransitionBinding.for_tool(
        agent_id="synthetic-billing-agent",
        policy_version="2026.11.0",
        side_effect_class=SideEffectClass.KEYED_MUTATE,
    )

    @ledger_sync(
        storage=FileLedgerStorage(ledger_path),
        transition_binding=binding,
        outcome_emitter=emitter,
    )
    def send_receipt(invoice_id: str) -> dict[str, str]:
        return {"status": "sent", "invoice_id": invoice_id}

    scope = TransitionScope(thread_id="synthetic-thread", run_id="synthetic-run-1")
    with execution_scope(scope):
        send_receipt(
            invoice_id="inv-synthetic-002",
            request_id="req-synthetic-completed-002",
        )


def main() -> None:
    print("=== Synthetic Outcome Telemetry and DTTR Walkthrough ===\n")
    print("Note: All events, request IDs, and provider refs below are synthetic.")
    print("DTTR measures silent duplicate tool-body executions divided by")
    print("long-running or redispatched transitions; it does NOT count prevented")
    print("external provider effects.\n")

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        outcome_path = tmp_root / "synthetic-outcomes.jsonl"
        ledger_path = tmp_root / "synthetic-ledger.json"
        emit_synthetic_outcomes(outcome_path, ledger_path)

        reloaded_storage = FileOutcomeStorage(outcome_path)
        reloaded_rows = reloaded_storage.list_all()
        report = compute_dttr_from_storage(reloaded_storage)

        print(f"Reloaded {len(reloaded_rows)} synthetic outcome rows from FileOutcomeStorage.")
        print(
            f"DTTR summary: dttr={report.dttr:.3f} "
            f"silent_duplicates={report.silent_duplicates} "
            f"long_running_or_redispatched={report.long_running_or_redispatched} "
            f"transitions={report.transitions}"
        )
        print("\nPer-transition breakdown:")
        for item in report.per_transition:
            role = (
                "in DTTR denominator (>=2 resolution events)"
                if item.long_running_or_redispatched
                else "excluded from DTTR denominator (single resolution, not long-running)"
            )
            print(
                f"  - {item.request_id} ({item.tool}): "
                f"body_executions={item.body_executions}, "
                f"authorized_reexecs={item.authorized_reexecs}, "
                f"silent_duplicates={item.silent_duplicates}, "
                f"resolution_events={item.resolution_events} -> {role}"
            )

        print("\nRetention and completeness note (see sdk/docs/LEDGER_PAYLOAD_STORAGE.md):")
        print("  Outcome retention is host-owned (issue #251); Mycelium keeps")
        print("  OutcomeStorage append-only and does not ship automatic outcome")
        print("  pruning. If host rotation or deletion removes initial block,")
        print("  resolution, or release boundary events, compute_dttr() observes")
        print("  only surviving rows and yields a partial retained-history metric")
        print("  rather than full-history DTTR.")


if __name__ == "__main__":
    main()
