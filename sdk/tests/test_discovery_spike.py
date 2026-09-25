"""End-to-end proof of the bounded discovery design in issue #116."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from mycelium.config_detect import detect_project

FIXTURE = Path(__file__).parent / "fixtures" / "discovery_spike"
SDK = Path(__file__).parents[1]


def _environment(calls: Path) -> dict[str, str]:
    previous = os.environ.get("PYTHONPATH")
    return {
        **os.environ,
        "PYTHONPATH": str(SDK) + (os.pathsep + previous if previous else ""),
        "DISCOVERY_SPIKE_CALLS": str(calls),
    }


def test_reviewed_inventory_matches_detected_and_runtime_boundaries(tmp_path: Path) -> None:
    project = tmp_path / "existing_app"
    shutil.copytree(FIXTURE, project)
    inventory = json.loads((project / "inventory.json").read_text(encoding="utf-8"))
    findings = {item["name"]: item["status"] for item in inventory["findings"]}
    assert findings == {
        "send_notice": "protected",
        "lookup_status": "unclassified",
        "ProviderClient.charge": "unsupported",
        "hidden_provider_call": "opaque",
    }

    detected = detect_project(project)
    assert {tool.name for tool in detected.tools} == {"send_notice", "lookup_status"}

    calls = tmp_path / "calls.jsonl"
    result = subprocess.run(
        [
            sys.executable, "-m", "mycelium", "run", "--config", str(project / "mycelium.yaml"),
            "--", sys.executable, "app.py",
        ],
        cwd=project,
        env=_environment(calls),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["first"] == output["second"] == {
        "recipient": "person@example.test", "status": "sent",
    }
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1


def test_repeated_instrumentation_preserves_one_wrapper(tmp_path: Path) -> None:
    project = tmp_path / "existing_app"
    shutil.copytree(FIXTURE, project)
    calls = tmp_path / "calls.jsonl"
    code = (
        "import tools\n"
        "from mycelium import load_config\n"
        "from mycelium.auto_instrumentation import instrument_configured_callables\n"
        "config = load_config('mycelium.yaml')\n"
        "instrument_configured_callables(config)\n"
        "first = tools.send_notice\n"
        "instrument_configured_callables(config)\n"
        "assert tools.send_notice is first\n"
        "tools.send_notice('person@example.test', request_id='notice-1')\n"
        "tools.send_notice('person@example.test', request_id='notice-1')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=project, env=_environment(calls),
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert len(calls.read_text(encoding="utf-8").splitlines()) == 1
