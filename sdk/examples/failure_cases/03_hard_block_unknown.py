"""03 — HARD_BLOCK: ambiguous mutate crash cannot blind-retry."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mycelium.proofs.feature_demo import prove_hard_block

from _narrate import narrate


def main() -> int:
    return narrate(
        case_id="03",
        title="Ambiguous mutate after provider timeout",
        gate="HARD_BLOCK",
        without="retry may double-charge if the first attempt actually committed",
        with_guard="redispatch raises LedgerHardBlockError; body runs once",
        prove=prove_hard_block,
    )


if __name__ == "__main__":
    raise SystemExit(main())
