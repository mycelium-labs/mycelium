"""AF-002 failure-case pack — gate repros for RETURN / POLL / HARD_BLOCK (+)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mycelium.proofs.feature_demo import (
    prove_hard_block,
    prove_lease_auto_renew,
    prove_reconcile_completed,
    prove_repair_gate,
    prove_return_completed,
)

_PACK = Path(__file__).resolve().parents[1] / "examples" / "failure_cases"


def test_prove_return_completed() -> None:
    result = prove_return_completed()
    assert result["gate"] == "RETURN"
    assert result["executions"] == 1
    assert result["result"] == {"charged": 10.0}


def test_case_01_return() -> None:
    result = prove_return_completed()
    assert result["gate"] == "RETURN"


def test_case_02_poll() -> None:
    result = prove_lease_auto_renew()
    assert result["peer_gate"] == "POLL"
    assert result["lease_validity"] == "HELD"


def test_case_03_hard_block() -> None:
    result = prove_hard_block()
    assert result["gate"] == "HARD_BLOCK"
    assert result["executions"] == 1


def test_case_04_repair() -> None:
    result = prove_repair_gate()
    assert result["executions"] == 1
    assert result["repaired_idempotency_key"]


def test_case_05_reconcile() -> None:
    result = prove_reconcile_completed()
    assert result["executions"] == 1
    assert result["result"] == {"charged": True}


def test_run_all_pack_exits_zero() -> None:
    script = _PACK / "run_all.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(_PACK.parent.parent),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "5/5 failure cases passed" in completed.stdout
    assert "RETURN" in completed.stdout
    assert "POLL" in completed.stdout
    assert "HARD_BLOCK" in completed.stdout
