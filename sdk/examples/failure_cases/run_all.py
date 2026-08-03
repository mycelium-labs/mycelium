"""Run the full AF-002 failure-case pack (in-process, no Redis).

From the sdk/ directory::

    python examples/failure_cases/run_all.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


CASES = (
    "01_return_completed.py",
    "02_poll_in_flight.py",
    "03_hard_block_unknown.py",
    "04_repair_incomplete.py",
    "05_reconcile_completed.py",
)


def _load_main(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    # Sibling imports (``_narrate``) resolve against this package dir.
    sys.modules[path.stem] = module
    spec.loader.exec_module(module)
    return module.main


def main() -> int:
    here = Path(__file__).resolve().parent
    # Ensure ``from _narrate import …`` works for case scripts.
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))

    print("Mycelium failure-case pack — RETURN / POLL / HARD_BLOCK (+ REPAIR / reconcile)")
    print("In-memory ledger only; no Redis/Postgres required.\n")

    for name in CASES:
        path = here / name
        print("-" * 60)
        code = _load_main(path)()
        if code != 0:
            print(f"FAIL {name} exit={code}", file=sys.stderr)
            return code
        print()

    print("=" * 60)
    print(f"OK — {len(CASES)}/{len(CASES)} failure cases passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
