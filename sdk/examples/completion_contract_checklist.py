"""AF-007 demo: host checklist must be marked before terminal.

Run from the sdk directory::

    python examples/completion_contract_checklist.py

Shows refuse → mark required → allow_with_warnings (optional still pending)
→ mark optional → allow.
"""

from __future__ import annotations

from mycelium.completion_contract import (
    CompletionContract,
    CompletionRefusedError,
    InMemoryCompletionStorage,
    wrap_final_message,
)
from mycelium.transition import TransitionScope, execution_scope


def main() -> None:
    contract = CompletionContract(
        InMemoryCompletionStorage(),
        required=["send_email", "write_pr"],
        optional=["post_slack"],
    )

    def emit_final(answer: str) -> str:
        return answer

    finalize = wrap_final_message(contract, emit_final)
    scope = TransitionScope(thread_id="demo", run_id="demo-run-1", node="end")
    print("=== AF-007 completion contract checklist ===\n")

    with execution_scope(scope):
        print("1) Attempt terminal with nothing marked")
        try:
            finalize("All done!")
        except CompletionRefusedError as exc:
            print(f"   REFUSE: pending required={exc.pending_required}")

        print("\n2) Mark send_email=success, write_pr=failed (honest)")
        contract.mark("send_email", "success")
        contract.mark("write_pr", "failed")

        print("3) Terminal again — optional post_slack still pending → warn + allow")
        result = contract.complete_run()
        assert result is not None
        print(f"   verdict={result.verdict} pending_optional={result.pending_optional}")
        print(f"   final message: {finalize('Partial but declared complete.')!r}")

        print("\n4) Mark optional abandoned with reason")
        contract.mark(
            "post_slack",
            "abandoned",
            reason="channel muted for this tenant",
        )
        result = contract.complete_run()
        assert result is not None
        print(f"   verdict={result.verdict}")


if __name__ == "__main__":
    main()
