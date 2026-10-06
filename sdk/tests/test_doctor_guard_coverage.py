"""Doctor distinguishes declared policy, observed wrappers, and unverified host facts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

from mycelium import DoctorStatus, load_config_from_string, run_doctor_on_config
from mycelium.doctor.render import render_human, render_json


def _check(report, identifier: str):
    return next(item for item in report.checks if item.id == identifier)


def _state_config(path: str, extra: str = ""):
    return load_config_from_string(f"""
tools:
  update:
    side_effect_class: keyed_mutate
state_authority:
  canonical_callable: {path}
  require_state_ref: true
{extra}
""")


def test_state_resolver_is_inspected_without_invocation(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("doctor_state_fixture")

    def resolver(*, tool, thread_id, run_id, kwargs):
        pytest.fail("Doctor must not call the host state resolver")

    module.resolver = resolver
    monkeypatch.setitem(sys.modules, module.__name__, module)
    report = run_doctor_on_config(_state_config(f"{module.__name__}:resolver"), connectivity=False)
    check = _check(report, "state_authority.callable")
    assert check.status == DoctorStatus.PASS
    assert "not invoked" in check.details
    assert _check(report, "state_authority.missing_ref").status == DoctorStatus.PASS
    assert _check(report, "state_authority.host_evidence").evidence == "not_verifiable"


@pytest.mark.parametrize("kind", ["missing", "not_callable", "wrong_signature", "async"])
def test_unusable_state_resolver_is_reported(kind: str, monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("doctor_bad_state_fixture")

    async def async_resolver(*, tool, thread_id, run_id, kwargs):
        pytest.fail("Must not invoke an async resolver")

    if kind != "missing":
        module.resolver = {
            "not_callable": 123,
            "wrong_signature": lambda account: "state",
            "async": async_resolver,
        }[kind]
    monkeypatch.setitem(sys.modules, module.__name__, module)
    report = run_doctor_on_config(_state_config(f"{module.__name__}:resolver"), connectivity=False)
    assert _check(report, "state_authority.callable").status == DoctorStatus.FAIL


def test_unknown_or_empty_state_selection_is_advisory(monkeypatch: pytest.MonkeyPatch) -> None:
    module = ModuleType("doctor_state_selection_fixture")
    module.resolver = lambda **kwargs: "state"
    monkeypatch.setitem(sys.modules, module.__name__, module)
    report = run_doctor_on_config(
        _state_config(f"{module.__name__}:resolver", "  tools: [misspelled_tool]"),
        connectivity=False,
    )
    check = _check(report, "state_authority.selection")
    assert check.status == DoctorStatus.WARN
    assert "misspelled_tool" in check.details
    assert not check.blocking


def test_absent_state_authority_does_not_invent_a_failure() -> None:
    report = run_doctor_on_config(load_config_from_string("{}"), connectivity=False)
    assert _check(report, "state_authority.configured").status == DoctorStatus.SKIP


@pytest.mark.parametrize("section", ["action_ledger", "state_backend", "loop_guard", "budget"])
def test_redis_durability_is_always_advisory(section: str, tmp_path: Path) -> None:
    limits = "  max_steps: 5\n" if section == "budget" else ""
    config = load_config_from_string(f"""
profile: production
deployment: {{topology: single_node}}
transition: {{agent_id: redis-advisory, policy_version: "1"}}
outcome_emit:
  storage: file
  path: {tmp_path / "outcomes.jsonl"}
  on_failure: error
{section}:
  storage: redis
  url: redis://localhost/0
{limits}
""")
    report = run_doctor_on_config(config, connectivity=False)
    check = _check(report, "redis.persistence")
    assert check.status == DoctorStatus.WARN
    assert check.evidence == "operator_asserted"
    assert not check.blocking
    assert section in check.details
    assert "Redis durability" in render_human(report)
    assert report.production_ready


@pytest.mark.parametrize("storage", ["memory", "file", "sqlite"])
def test_non_redis_storage_does_not_get_persistence_warning(storage: str, tmp_path: Path) -> None:
    config = load_config_from_string(
        f"action_ledger:\n  storage: {storage}\n  path: {tmp_path / 'ledger'}\n"
    )
    report = run_doctor_on_config(config, connectivity=False)
    assert not any(item.id == "redis.persistence" for item in report.checks)


def test_inventory_separates_observed_unwrapped_and_unverified_tools(tmp_path: Path) -> None:
    config = load_config_from_string(f"""
transition:
  agent_id: coverage
  policy_version: "1"
tools:
  send:
    ledger: {{storage: sqlite, path: {tmp_path / "ledger.db"}}}
    side_effect_class: keyed_mutate
    request_id_from: order_id
  unseen:
    side_effect_class: read
""")
    calls = []

    def send(**kwargs):
        calls.append(kwargs)
        pytest.fail("Doctor must not execute tools")

    def hidden(**kwargs):
        calls.append(kwargs)
        pytest.fail("Doctor must not execute unconfigured tools")

    wrapped = config.apply(send)
    report = run_doctor_on_config(
        config, connectivity=False, observed_tools={"send": wrapped, "hidden": hidden}
    )
    rows = {row["tool"]: row for row in json.loads(render_json(report))["protection_coverage"]}
    assert rows["send"]["wrapper_status"] == "wrapper_observed"
    assert rows["send"]["durable_ledger_configured"]
    assert rows["send"]["request_identity"] == "host_field_configured"
    assert rows["send"]["provider_boundary"] == "unverified"
    assert rows["send"]["recovery_path"] == "unverified"
    assert rows["unseen"]["wrapper_status"] == "unverified"
    assert rows["hidden"]["wrapper_status"] == "unprotected"
    assert not rows["hidden"]["configured"]
    assert "tool_configuration" in rows["hidden"]["missing_requirements"]
    assert not calls


def test_configuration_does_not_prove_a_tool_is_wrapped() -> None:
    config = load_config_from_string("tools: {plain: {side_effect_class: read}}")
    report = run_doctor_on_config(
        config, connectivity=False, observed_tools={"plain": lambda: None}
    )
    assert report.protection_coverage[0]["wrapper_status"] == "unprotected"
