"""Guard option typos must fail before activation without changing valid configs."""

from __future__ import annotations

import pytest

from mycelium import ConfigError, config_json_schema, load_config_from_string
from mycelium.config_schema import validate_config_shape

SECTIONS = ("budget", "completion", "loop_guard", "scope_guard", "history_guard")


@pytest.mark.parametrize("section", SECTIONS)
@pytest.mark.parametrize("value", ["true", "false", '"false"'])
def test_unsupported_enabled_fails_before_guard_activation(section: str, value: str) -> None:
    with pytest.raises(ConfigError, match=rf"{section}\.enabled.*omit"):
        load_config_from_string(f"{section}:\n  enabled: {value}\n")


@pytest.mark.parametrize("section", SECTIONS)
def test_unknown_guard_option_reports_full_path(section: str) -> None:
    with pytest.raises(ConfigError, match=rf"{section}\.totally_fake"):
        load_config_from_string(f"{section}:\n  totally_fake: 1\n")


def test_omitted_guards_remain_disabled() -> None:
    config = load_config_from_string("tools: {}")
    assert config.build_budget_guard() is None
    assert config.build_completion_contract() is None
    assert config.build_loop_guard() is None
    assert config.build_scope_guard() is None
    assert config.build_history_guard() is None


def test_documented_guard_options_remain_loadable_and_effective() -> None:
    config = load_config_from_string("""
tools:
  search:
    side_effect_class: read
budget:
  max_steps: 5
  tools: [search]
  exclude: []
loop_guard:
  consecutive_soft: {read: 4}
  escalate_after_soft: 2
  missing_run_id_policy: error
scope_guard:
  allowed_tools: [search]
  auto_bind: false
completion:
  required: [done]
  optional: [{id: receipt}]
history_guard:
  max_messages: 10
  detect_duplicates: false
""")
    assert config.budget_guard_applies("search")
    assert not config.budget_guard_applies("other")
    assert config.build_budget_guard() is not None
    assert config.build_loop_guard() is not None
    assert config.build_scope_guard() is not None
    assert config.build_completion_contract() is not None
    assert config.build_history_guard() is not None
    assert config.history_guard["detect_duplicates"] is False
    assert config.scope_guard["auto_bind"] is False


@pytest.mark.parametrize("section", SECTIONS)
def test_schema_rejects_the_same_unknown_options(section: str) -> None:
    schema = config_json_schema()
    reference = schema["properties"][section]["anyOf"][0]["$ref"].rsplit("/", 1)[-1]
    assert schema["$defs"][reference]["additionalProperties"] is False
    with pytest.raises(ValueError, match="totally_fake"):
        validate_config_shape({section: {"totally_fake": 1}})
