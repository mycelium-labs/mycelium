import pytest

from mycelium import ConfigError, load_config_from_string


@pytest.mark.parametrize(
    "field,value",
    [
        (field, value)
        for field in ("max_tokens", "max_messages")
        for value in ("true", "false", "0", "-1", "1.5", '"10"')
    ]
    + [("warn_at", value) for value in (".nan", ".inf", "-.inf", "true", "0", "1.1", "null")]
    + [("detect_duplicates", value) for value in ('"false"', "0", "null")],
)
def test_history_guard_invalid_scalars(field, value):
    with pytest.raises(ConfigError, match=rf"history_guard\.{field}"):
        load_config_from_string(f"history_guard:\n  {field}: {value}\n")


@pytest.mark.parametrize(
    "yaml",
    [
        "{}",
        "{max_tokens: null, max_messages: null}",
        "{max_tokens: 100, max_messages: 10, warn_at: 0.5, detect_duplicates: false}",
        "{warn_at: 1, detect_duplicates: true}",
    ],
)
def test_history_guard_valid_scalars(yaml):
    cfg = load_config_from_string(f"history_guard: {yaml}\n")
    assert cfg.build_history_guard() is not None
