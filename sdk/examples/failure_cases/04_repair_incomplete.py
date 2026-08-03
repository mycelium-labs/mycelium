"""04 — REPAIR: incomplete durable record is healed; no second side effect."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mycelium.proofs.feature_demo import prove_repair_gate

from _narrate import narrate


def main() -> int:
    return narrate(
        case_id="04",
        title="Incomplete durable record on redispatch",
        gate="REPAIR",
        without="missing idempotency_key / terminal fields can fork identity",
        with_guard="claim loop repairs fields then RETURN; body still once",
        prove=prove_repair_gate,
    )


if __name__ == "__main__":
    raise SystemExit(main())
