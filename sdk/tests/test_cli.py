"""Tests for the mycelium CLI."""

from __future__ import annotations

from pathlib import Path

from mycelium.__main__ import main


def test_init_writes_quickstart_template_by_default(tmp_path: Path) -> None:
    out = tmp_path / "mycelium.yaml"
    assert main(["init", "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "action_ledger:" in text
    assert "subagent_task" in text
    assert "send_payment" not in text


def test_init_writes_full_template(tmp_path: Path) -> None:
    out = tmp_path / "mycelium.yaml"
    assert main(["init", "--full", "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "action_ledger:" in text
    assert "send_payment" in text


def test_init_minimal_template(tmp_path: Path) -> None:
    out = tmp_path / "mycelium.yaml"
    assert main(["init", "--minimal", "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "action_ledger:" in text
    assert "send_payment" not in text
    assert "subagent_task" not in text


def test_demo_runs(capsys) -> None:
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "Without Mycelium" in out
    assert "With Mycelium" in out
    assert "langgraph/issues/7417" in out


def test_init_refuses_overwrite(tmp_path: Path) -> None:
    out = tmp_path / "mycelium.yaml"
    out.write_text("existing", encoding="utf-8")
    assert main(["init", "-o", str(out)]) == 1
    assert out.read_text(encoding="utf-8") == "existing"


def test_init_force_overwrite(tmp_path: Path) -> None:
    out = tmp_path / "mycelium.yaml"
    out.write_text("existing", encoding="utf-8")
    assert main(["init", "-o", str(out), "--force"]) == 0
    assert "action_ledger:" in out.read_text(encoding="utf-8")
