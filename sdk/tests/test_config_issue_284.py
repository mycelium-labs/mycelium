"""Issue #284: ``registry.auto`` was read with a plain truthiness check.

A quoted ``"false"`` is a truthy string, so it enabled automatic registration
and populated the allowlist with every configured tool -- the opposite of what
the config said. ``RegistryConfigModel.auto`` in config_schema.py is already
typed ``bool``, so the generated schema promised a strictness the parser did
not enforce.
"""

import pytest

from mycelium import ConfigError, load_config_from_string

TOOLS = "tools:\n  demo: {}\n"


@pytest.mark.parametrize(
    "value",
    [
        '"false"',
        "'false'",
        '"true"',
        '"no"',
        "0",
        "1",
        "1.0",
        "null",
        "[]",
        "[true]",
        "{}",
        "{enabled: true}",
    ],
)
def test_registry_auto_rejects_non_booleans(value: str) -> None:
    with pytest.raises(ConfigError, match=r"registry\.auto"):
        load_config_from_string(f"{TOOLS}registry:\n  auto: {value}\n")


def test_registry_auto_false_leaves_the_allowlist_empty() -> None:
    cfg = load_config_from_string(f"{TOOLS}registry:\n  auto: false\n")
    assert cfg.registry_allowed == []


def test_registry_auto_true_registers_every_configured_tool() -> None:
    cfg = load_config_from_string(f"{TOOLS}registry:\n  auto: true\n")
    assert cfg.registry_allowed == ["demo"]


def test_omitting_registry_auto_keeps_the_default() -> None:
    assert load_config_from_string(TOOLS).registry_allowed == []
    cfg = load_config_from_string(f"{TOOLS}registry:\n  allowed: []\n")
    assert cfg.registry_allowed == []


@pytest.mark.parametrize("auto", ["true", "false"])
def test_an_explicit_allowlist_wins_over_auto(auto: str) -> None:
    two_tools = "tools:\n  demo: {}\n  other: {}\n"
    cfg = load_config_from_string(two_tools + f"registry:\n  allowed: [demo]\n  auto: {auto}\n")
    assert cfg.registry_allowed == ["demo"]
