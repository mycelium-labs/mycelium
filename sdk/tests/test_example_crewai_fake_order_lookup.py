"""Run the CrewAI integration example when the optional extra is installed."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

SDK_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = SDK_ROOT / "examples" / "crewai_fake_order_lookup" / "run.py"
MARKER = (
    "crewai integration walkthrough complete: status=verified "
    "run_id_bound=True completion_refused=True completion_accepted=True "
    "tool_calls=1"
)


def test_crewai_example_needs_no_credentials_or_network(tmp_path: Path) -> None:
    pytest.importorskip("crewai", reason="requires the optional crewai extra")

    completed = subprocess.run(  # noqa: S603 - fixed interpreter, repo-owned path
        [sys.executable, str(EXAMPLE)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    output = completed.stdout + "\n" + completed.stderr
    assert completed.returncode == 0, f"{EXAMPLE.name} exited {completed.returncode}:\n{output}"
    assert MARKER in completed.stdout, f"expected {MARKER!r} on stdout:\n{output}"
