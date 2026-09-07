"""Compatibility entry point for ``python -m mycelium`` and the console script."""

from __future__ import annotations

from importlib import import_module

# These names were historically imported from __main__ by a few integrations.
# Keep them available without making the command implementation eager.
_LAZY_IMPORTS = {
    **{
        name: ("mycelium.cli.commands", name)
        for name in (
            "_ENV_ADAPTER_REPORT_SIGNING_KEY", "_ENV_LEDGER_FILE", "_ENV_OUTCOME_FILE",
            "_ENV_POSTGRES_DSN", "_ENV_REDIS_URL", "_ENV_SQLITE_PATH",
            "_TEMPLATE_FULL", "_TEMPLATE_MINIMAL", "_TEMPLATE_QUICKSTART",
            "cmd_budget_release", "cmd_budget_status", "cmd_completion_mark",
            "cmd_completion_status", "cmd_config_docs", "cmd_config_example",
            "cmd_config_schema", "cmd_demo", "cmd_doctor", "cmd_init", "cmd_loops_release",
            "cmd_coverage", "cmd_preview",
            "cmd_loops_status", "cmd_outcomes_dttr", "cmd_providers_verify",
            "cmd_providers_verify_report", "cmd_run", "cmd_scope_bind", "cmd_scope_status",
            "cmd_sidecar_serve", "cmd_skills_install", "cmd_verify",
        )
    },
    "build_parser": ("mycelium.cli.parser", "build_parser"),
    "dispatch": ("mycelium.cli.parser", "dispatch"),
    "run_cli": ("mycelium.cli.parser", "run_cli"),
    "cmd_migrate": ("mycelium.cli_migrations", "cmd_migrate"),
    "cmd_state_migrate": ("mycelium.cli_migrations", "cmd_state_migrate"),
    "_add_operator_storage_args": ("mycelium.cli_transitions", "_add_operator_storage_args"),
    "cmd_transitions_export": ("mycelium.cli_transitions", "cmd_transitions_export"),
    "cmd_transitions_list": ("mycelium.cli_transitions", "cmd_transitions_list"),
    "cmd_transitions_mark_dead": ("mycelium.cli_transitions", "cmd_transitions_mark_dead"),
    "cmd_transitions_prune": ("mycelium.cli_transitions", "cmd_transitions_prune"),
    "cmd_transitions_release": ("mycelium.cli_transitions", "cmd_transitions_release"),
    "cmd_transitions_show": ("mycelium.cli_transitions", "cmd_transitions_show"),
    "TerminalOutcome": ("mycelium.transition", "TerminalOutcome"),
}


def __getattr__(name: str):
    try:
        module_name, attribute_name = _LAZY_IMPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value


def main(argv: list[str] | None = None) -> int:
    """Run the CLI, importing its comparatively heavy implementation on demand."""
    return __getattr__("run_cli")(argv)


if __name__ == "__main__":
    raise SystemExit(main())
