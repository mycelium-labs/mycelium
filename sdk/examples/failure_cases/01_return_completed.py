"""01 — RETURN: completed redispatch returns stored result (no second body run)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mycelium.proofs.feature_demo import prove_return_completed

from _narrate import narrate


def main() -> int:
    return narrate(
        case_id="01",
        title="Completed redispatch",
        gate="RETURN",
        without="same tool_call_id runs the charge again",
        with_guard="second call returns the stored COMPLETED result",
        prove=prove_return_completed,
    )


if __name__ == "__main__":
    raise SystemExit(main())
