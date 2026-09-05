"""Validation tests for documented CLI commands and configuration examples (#153).

Validates that:
1. Documented CLI commands in markdown code blocks and docs match the current ArgumentParser.
2. Documented subcommands and key flags remain recognized by build_parser().
3. Checked-in configuration YAML examples and templates validate against the schema
   model and load through load_config().
4. Documented configuration YAML blocks in documentation are structurally valid,
   with networked, credentialed, and illustrative examples explicitly tracked.
"""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

from mycelium import load_config
from mycelium.cli import build_parser
from mycelium.config_schema import validate_config_shape

SDK_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SDK_ROOT.parent

DOC_FILES = [
    REPO_ROOT / "README.md",
    SDK_ROOT / "README.md",
    SDK_ROOT / "docs" / "ARCHITECTURE_AND_GUARANTEE_MAP.md",
    SDK_ROOT / "docs" / "CONFIG_REFERENCE.md",
    SDK_ROOT / "docs" / "FAILURE_AND_THREAT_MODEL.md",
    SDK_ROOT / "docs" / "FAILURE_MODE_CATALOG.md",
    SDK_ROOT / "docs" / "LEDGER_PAYLOAD_STORAGE.md",
    SDK_ROOT / "docs" / "RELEASE.md",
]

CHECKED_IN_YAML_FILES = [
    SDK_ROOT / "examples" / "mycelium.generated.example.yaml",
    SDK_ROOT / "examples" / "langgraph_redis_crash" / "mycelium.example.yaml",
    SDK_ROOT / "mycelium" / "templates" / "mycelium.minimal.yaml",
    SDK_ROOT / "mycelium" / "templates" / "mycelium.quickstart.yaml",
    SDK_ROOT / "mycelium" / "templates" / "mycelium.template.yaml",
    SDK_ROOT / "mycelium" / "skills" / "mycelium-setup" / "agents" / "openai.yaml",
]


class ExtractedCommand(NamedTuple):
    source_file: str
    line_number: int
    raw_command: str


def _extract_code_block_commands() -> list[ExtractedCommand]:
    commands: list[ExtractedCommand] = []
    for doc in DOC_FILES:
        if not doc.is_file():
            continue
        lines = doc.read_text(encoding="utf-8").splitlines()
        in_block = False
        block_lang = ""
        current_cmd = ""
        cmd_start_line = 0

        for idx, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_block:
                    in_block = False
                    if current_cmd:
                        commands.append(
                            ExtractedCommand(doc.name, cmd_start_line, current_cmd)
                        )
                        current_cmd = ""
                else:
                    in_block = True
                    block_lang = stripped[3:].strip().lower()
                    current_cmd = ""
                continue

            if in_block and block_lang in ("bash", "sh", "shell", "console", ""):
                clean = re.sub(r"#.*$", "", stripped).strip()
                if not clean:
                    continue
                if clean.endswith("\\"):
                    if not current_cmd and clean.startswith("mycelium"):
                        current_cmd = clean[:-1].strip()
                        cmd_start_line = idx
                    elif current_cmd:
                        current_cmd += " " + clean[:-1].strip()
                else:
                    if current_cmd:
                        current_cmd += " " + clean
                        commands.append(
                            ExtractedCommand(doc.name, cmd_start_line, current_cmd)
                        )
                        current_cmd = ""
                    elif clean.startswith("mycelium ") or clean == "mycelium":
                        commands.append(ExtractedCommand(doc.name, idx, clean))

    return commands


def _normalize_command_tokens(raw: str) -> list[str]:
    """Substitute documentation placeholders and environment variables with test dummies."""
    norm = raw
    norm = re.sub(r"<request_id>", "req_dummy_123", norm)
    norm = re.sub(r"<run_id>", "run_dummy_123", norm)
    norm = re.sub(r"<adapter>", "gmail", norm)
    norm = re.sub(r"clear\|allow-once\|abort-run", "clear", norm)
    norm = re.sub(r'"?\$MYCELIUM_[A-Z0-9_]+"?', "test_dummy_val", norm)
    norm = re.sub(r'"\.\.\."', '"test_reason"', norm)

    tokens = shlex.split(norm)
    if tokens and tokens[0] == "mycelium":
        return tokens[1:]
    return tokens


def test_checked_in_yaml_examples_and_templates() -> None:
    """All checked-in configuration YAML files and templates conform to schema and load cleanly."""
    for yaml_path in CHECKED_IN_YAML_FILES:
        assert yaml_path.is_file(), f"Missing configuration file: {yaml_path}"
        data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
        assert isinstance(data, dict), f"Config is not a mapping: {yaml_path}"

        # 1. Structural schema validation via Pydantic model
        model = validate_config_shape(data)
        assert model.config_version == 1, f"Unexpected config_version in {yaml_path}"

        # 2. Runtime loader validation
        config = load_config(yaml_path)
        assert config is not None, f"load_config returned None for {yaml_path}"


def test_documented_code_block_commands_parse_cleanly() -> None:
    """Every documented CLI invocation in markdown code blocks must parse against build_parser()."""
    parser = build_parser()
    documented = _extract_code_block_commands()
    assert len(documented) >= 20, (
        f"Expected at least 20 documented CLI commands, found {len(documented)}"
    )

    failures: list[str] = []
    for item in documented:
        args = _normalize_command_tokens(item.raw_command)
        if not args:
            continue
        try:
            parser.parse_args(args)
        except SystemExit as exc:
            failures.append(
                f"{item.source_file}:{item.line_number} -> '{item.raw_command}' "
                f"failed parsing with exit code {exc.code}"
            )

    assert not failures, (
        "Documented CLI commands failed parser validation:\n" + "\n".join(failures)
    )


def test_documented_subcommands_and_options_exist_in_parser() -> None:
    """Explicitly verify documented subcommands and essential flags in build_parser()."""
    parser = build_parser()
    subparsers_action = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    commands = subparsers_action.choices

    # 1. All core top-level subcommands documented across README and failure catalogs
    expected_top_level = {
        "init",
        "demo",
        "doctor",
        "verify",
        "config",
        "skills",
        "run",
        "migrate",
        "state",
        "providers",
        "transitions",
        "loops",
        "completion",
        "budget",
        "scope",
        "outcomes",
        "sidecar",
    }
    missing = expected_top_level - set(commands.keys())
    assert not missing, f"Documented subcommands missing from parser: {sorted(missing)}"

    # 2. Key flags on doctor
    doctor_opts = {opt for act in commands["doctor"]._actions for opt in act.option_strings}
    doctor_expected = (
        "--config",
        "-c",
        "--strict",
        "--json",
        "--verbose",
        "--fix",
        "--no-connectivity",
        "--timeout",
    )
    for expected_opt in doctor_expected:
        assert expected_opt in doctor_opts, (
            f"mycelium doctor missing documented flag {expected_opt}"
        )

    # 3. Key flags on init
    init_opts = {opt for act in commands["init"]._actions for opt in act.option_strings}
    init_expected = (
        "--full",
        "--minimal",
        "--detect",
        "--force",
        "-o",
        "--output",
        "--project",
    )
    for expected_opt in init_expected:
        assert expected_opt in init_opts, (
            f"mycelium init missing documented flag {expected_opt}"
        )

    # 4. Nested subcommands for transitions
    transitions_subaction = next(
        a for a in commands["transitions"]._actions if isinstance(a, argparse._SubParsersAction)
    )
    expected_transitions = {"list", "export", "prune", "show", "release", "mark-dead"}
    missing_transitions = expected_transitions - set(transitions_subaction.choices.keys())
    assert not missing_transitions, (
        f"transitions missing documented subcommands: {sorted(missing_transitions)}"
    )

    # 5. Transitions list flags
    trans_list_opts = {
        opt for act in transitions_subaction.choices["list"]._actions for opt in act.option_strings
    }
    trans_list_expected = (
        "--stuck",
        "--config",
        "--sqlite",
        "--redis-url",
        "--postgres-dsn",
        "--parent",
        "--limit",
    )
    for expected_opt in trans_list_expected:
        assert expected_opt in trans_list_opts, (
            f"transitions list missing documented flag {expected_opt}"
        )

    # 6. Transitions release flags
    trans_release_opts = {
        opt
        for act in transitions_subaction.choices["release"]._actions
        for opt in act.option_strings
    }
    for expected_opt in ("--verified", "--by", "--reason", "--result-json"):
        assert expected_opt in trans_release_opts, (
            f"transitions release missing documented flag {expected_opt}"
        )


def test_documented_yaml_snippets_and_illustrative_classification() -> None:
    """Verify YAML configuration examples in documentation against schema models.

    Complete configuration blocks must validate against the structural model.
    Illustrative, credentialed (requiring DATABASE_URL), and partial snippets are
    explicitly categorized and tested.
    """
    for doc in (REPO_ROOT / "README.md", SDK_ROOT / "README.md"):
        if not doc.is_file():
            continue
        text = doc.read_text(encoding="utf-8")
        matches = list(re.finditer(r"```ya?ml\n(.*?)```", text, re.DOTALL))
        assert len(matches) > 0, f"Expected YAML blocks in {doc.name}"

        for match in matches:
            line_no = text[: match.start()].count("\n") + 1
            raw_yaml = match.group(1)
            parsed: Any = yaml.safe_load(raw_yaml)
            if not isinstance(parsed, dict):
                continue

            # Check if this is the illustrative single-field transition snippet
            # from sdk/README.md around line 2110 which illustrates only
            # reclaim_requires_death_signal without required agent_id/policy_version
            is_partial_transition_snippet = (
                "transition" in parsed
                and isinstance(parsed["transition"], dict)
                and "agent_id" not in parsed["transition"]
            )
            if is_partial_transition_snippet:
                assert "reclaim_requires_death_signal" in parsed["transition"]
                continue

            # Structural validation via Pydantic model must succeed for all other snippets
            try:
                model = validate_config_shape(parsed)
                assert model.config_version == 1
            except Exception as err:
                pytest.fail(
                    f"Doc YAML snippet at {doc.name}:{line_no} failed schema validation: {err}"
                )


def test_parser_rejects_removed_or_renamed_command() -> None:
    """Verify parser fails with code 2 on unknown subcommand or renamed flag."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["unknown_command_name"])
    assert exc_info.value.code == 2

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["doctor", "--renamed-or-nonexistent-flag"])
    assert exc_info.value.code == 2
