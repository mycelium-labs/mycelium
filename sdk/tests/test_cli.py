"""Tests for the mycelium CLI."""

from __future__ import annotations

from pathlib import Path

from mycelium import load_config
from mycelium.__main__ import main
from mycelium.transition import SideEffectClass


def test_init_writes_quickstart_template_by_default(tmp_path: Path) -> None:
    out = tmp_path / "mycelium.yaml"
    assert main(["init", "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "transition:" in text
    assert "integrations:" in text
    assert "langgraph:" in text
    assert "enabled: true" in text
    assert "action_ledger:" in text
    assert "subagent_task" in text
    assert "side_effect_class: non_idempotent_mutate" in text
    assert "send_payment" not in text

    config = load_config(out)
    assert config.transition is not None
    assert config.langgraph_enabled
    assert config.transition.agent_id.startswith("<TODO:")
    assert "agent id" in config.transition.agent_id

    assert config.tools["subagent_task"].side_effect_class == SideEffectClass.NON_IDEMPOTENT_MUTATE


def test_init_writes_full_template(tmp_path: Path) -> None:
    out = tmp_path / "mycelium.yaml"
    assert main(["init", "--full", "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "transition:" in text
    assert "action_ledger:" in text
    assert "tools: {}" in text
    assert "tasks: {}" in text
    assert "side_effect_class: read" in text
    assert "side_effect_class: idempotent_mutate" in text
    assert "side_effect_class: keyed_mutate" in text
    assert "side_effect_class: non_idempotent_mutate" in text
    assert "side_effect_class: irreversible" in text
    assert "spendability" in text
    assert "multi_use" in text
    assert "non_replayable" in text
    assert "<TODO: your_read_tool>" not in text
    assert "retry_permission: manual_reconciliation_required" not in text

    config = load_config(out)
    assert config.transition is not None
    assert config.tools == {}
    assert config.tasks == {}
    assert config.registry_allowed == []


def test_init_minimal_template(tmp_path: Path) -> None:
    out = tmp_path / "mycelium.yaml"
    assert main(["init", "--minimal", "-o", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "transition:" in text
    assert "action_ledger:" in text
    assert "send_payment" not in text
    assert "subagent_task" not in text
    assert load_config(out).transition is not None


def test_demo_runs(capsys) -> None:
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "Mycelium proof demo (real test)" in out
    assert "langgraph-7417-duplicate-execution" in out
    assert "langgraph/issues/7417" in out
    assert "PASS" in out
    assert "transition envelope (v1.3)" in out
    assert "side_effect_class: non_idempotent_mutate" in out
    assert "load_config" in out
    assert "@ledger_sync()" not in out
    assert "@config.apply" in out


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
