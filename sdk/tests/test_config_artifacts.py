"""Generated config artifacts stay synchronized with the typed model."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

from mycelium import config_parser, config_schema, load_config
from mycelium.config_artifacts import render_config_example, render_config_reference
from mycelium.config_schema import config_json_schema
from mycelium.entity_guard import DEST_TYPES

SDK_ROOT = Path(__file__).resolve().parents[1]


def test_checked_in_config_artifacts_are_current(tmp_path: Path) -> None:
    reference = render_config_reference()
    example = render_config_example()
    assert (SDK_ROOT / "docs" / "CONFIG_REFERENCE.md").read_text(
        encoding="utf-8"
    ) == reference
    assert (SDK_ROOT / "examples" / "mycelium.generated.example.yaml").read_text(
        encoding="utf-8"
    ) == example

    path = tmp_path / "mycelium.yaml"
    path.write_text(example, encoding="utf-8")
    config = load_config(path)
    assert config.transition is not None
    assert config.transition.agent_id == "example-agent"


def test_budget_schema_explains_max_steps_unit() -> None:
    schema = config_json_schema()
    budget_schema = schema["$defs"]["BudgetConfigModel"]
    description = budget_schema["properties"]["max_steps"]["description"]

    assert "budget-guarded tool invocation" in description
    assert "instrumented LLM turn" in description
    assert "business workflow counters are separate" in description


@pytest.mark.parametrize(
    ("model_name", "parser_keys"),
    [
        ("AuthorityWindowConfigModel", "_AUTHORITY_WINDOW_KEYS"),
        ("UseTimeCurrencyConfigModel", "_USE_TIME_TOP_KEYS"),
        ("UseTimeFactConfigModel", "_USE_TIME_FACT_KEYS"),
        ("FactSubjectConfigModel", "_USE_TIME_SUBJECT_KEYS"),
        ("DestructiveConfirmConfigModel", "_DESTRUCTIVE_TOP_KEYS"),
        ("DestructiveToolConfigModel", "_DESTRUCTIVE_TOOL_KEYS"),
        ("DestructiveObjectConfigModel", "_DESTRUCTIVE_OBJECT_KEYS"),
        ("DestructiveGrantConfigModel", "_DESTRUCTIVE_GRANT_KEYS"),
    ],
)
def test_generated_control_fields_match_semantic_parser(model_name, parser_keys) -> None:
    model = getattr(config_schema, model_name)
    assert set(model.model_fields) == getattr(config_parser, parser_keys)


def test_generated_destination_types_match_supported_runtime_types() -> None:
    annotation = config_schema.EntityDestinationConfigModel.model_fields["type"].annotation
    assert set(get_args(annotation)) == DEST_TYPES


@pytest.mark.parametrize("allow", [None, [], {}])
def test_empty_destination_allow_forms_preserve_legacy_compatibility(allow) -> None:
    model = config_schema.EntityDestinationConfigModel.model_validate(
        {"path": "recipient", "type": "email", "allow": allow}
    )
    assert model.allow.addresses == []
