"""05 — Reconcile COMPLETED: provider proof turns ambiguity into RETURN."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mycelium.proofs.feature_demo import prove_reconcile_completed

from _narrate import narrate


def main() -> int:
    return narrate(
        case_id="05",
        title="Ambiguous mutate + provider reconcile",
        gate="RETURN (via Reconciler COMPLETED)",
        without="stuck HARD_BLOCK or unsafe blind retry",
        with_guard="reconciler proves COMPLETED; stored result returned, no re-exec",
        prove=prove_reconcile_completed,
    )


if __name__ == "__main__":
    raise SystemExit(main())
