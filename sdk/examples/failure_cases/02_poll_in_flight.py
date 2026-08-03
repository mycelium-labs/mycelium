"""02 — POLL: peer sees HELD lease while owner still running (no parallel fire)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mycelium.proofs.feature_demo import prove_lease_auto_renew

from _narrate import narrate


def main() -> int:
    return narrate(
        case_id="02",
        title="Peer while owner lease held",
        gate="POLL",
        without="second worker charges while the first is still in flight",
        with_guard="peer gate is POLL; lease validity stays HELD via auto-renew",
        prove=prove_lease_auto_renew,
    )


if __name__ == "__main__":
    raise SystemExit(main())
