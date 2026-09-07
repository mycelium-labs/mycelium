"""CLI parser construction, dispatch, and exit-code routing."""

# Local imports below intentionally bind parser callbacks; ruff cannot see their
# use through argparse registration.
# ruff: noqa: F401, I001

from __future__ import annotations

import argparse
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    # Import command implementations only while constructing a CLI parser.
    # Importing this module is also used by library tooling and should stay cheap.
    from mycelium.ledger_model import LEDGER_ENTRY_SCHEMA_VERSION
    from mycelium.cli.commands import (
        _ENV_ADAPTER_REPORT_SIGNING_KEY,
        _ENV_OUTCOME_FILE,
        cmd_budget_release,
        cmd_budget_status,
        cmd_completion_mark,
        cmd_completion_status,
        cmd_config_docs,
        cmd_config_example,
        cmd_config_schema,
        cmd_demo,
        cmd_doctor,
        cmd_init,
        cmd_coverage,
        cmd_preview,
        cmd_loops_release,
        cmd_loops_status,
        cmd_outcomes_dttr,
        cmd_providers_verify,
        cmd_providers_verify_report,
        cmd_run,
        cmd_scope_bind,
        cmd_scope_status,
        cmd_sidecar_serve,
        cmd_skills_install,
        cmd_verify,
    )
    from mycelium.cli_migrations import cmd_migrate, cmd_state_migrate
    from mycelium.cli_transitions import (
        _add_operator_storage_args,
        cmd_transitions_export,
        cmd_transitions_list,
        cmd_transitions_mark_dead,
        cmd_transitions_prune,
        cmd_transitions_release,
        cmd_transitions_show,
    )
    from mycelium.transition import TerminalOutcome

    parser = argparse.ArgumentParser(
        prog="mycelium",
        description="Mycelium runtime: scaffold config and utilities",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sidecar_parser = sub.add_parser(
        "sidecar",
        help="Run the authenticated development-only local sidecar",
    )
    sidecar_sub = sidecar_parser.add_subparsers(dest="sidecar_command", required=True)
    sidecar_serve = sidecar_sub.add_parser(
        "serve", help="Serve the configured loopback HTTP adapter"
    )
    sidecar_serve.add_argument(
        "-c", "--config", type=Path, required=True, help="Absolute sidecar YAML configuration"
    )
    sidecar_serve.set_defaults(handler=cmd_sidecar_serve)

    doctor_parser = sub.add_parser(
        "doctor",
        help="Verify production safety configuration and wiring",
        description=(
            "Verification that Mycelium protections are actually "
            "configured and detectably wired — not merely installed. Never "
            "executes application tools, never calls an LLM, never writes "
            "ledger/outcome rows. It is read-only unless --fix is supplied; "
            "fixes are limited to version/schema metadata. "
            "CI gate: mycelium doctor --config mycelium.yaml --strict --json"
        ),
    )
    doctor_parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path (default: ./mycelium.yaml)",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable JSON for CI",
    )
    doctor_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as failures (exit 1)",
    )
    doctor_parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include check ids, evidence kinds, and details",
    )
    doctor_parser.add_argument(
        "--no-connectivity",
        action="store_true",
        help="Skip safe backend connectivity probes",
    )
    doctor_parser.add_argument(
        "--fix",
        action="store_true",
        help="Add only safe config-version and IDE-schema metadata, then verify",
    )
    doctor_parser.add_argument(
        "--timeout",
        type=float,
        default=2.0,
        help="Connectivity probe timeout in seconds (default: 2)",
    )

    coverage_parser = sub.add_parser("coverage", help="Inspect configured protection coverage")
    coverage_parser.add_argument("-c", "--config", type=Path, default=Path("mycelium.yaml"))
    coverage_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    preview_parser = sub.add_parser("preview", help="Safely preview a proposed tool action")
    preview_parser.add_argument("--tool", required=True, help="Configured tool name")
    preview_parser.add_argument(
        "--args-file", type=Path, required=True, help="JSON object containing tool arguments"
    )
    preview_parser.add_argument("-c", "--config", type=Path, default=Path("mycelium.yaml"))
    preview_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")

    verify_parser = sub.add_parser(
        "verify",
        help="Empirically verify guarantees with synthetic or opt-in cluster scenarios",
        description=(
            "Run Doctor, then exercise Mycelium's production guarantees against "
            "the configured storage backend using synthetic operations only. "
            "Normal mode never executes application tools, calls an LLM, or contacts "
            "a provider. Optional --cluster requires an explicitly configured sandbox. "
            "CI: mycelium verify --config mycelium.yaml --scenario all --strict --json"
        ),
    )
    verify_parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path (default: ./mycelium.yaml)",
    )
    verify_parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Scenario to run (repeatable): redispatch, contention, "
            "worker-crash, storage-outage, ambiguous-effect, reconcile, "
            "secret-in-args, entity-guard, destructive-confirm, "
            "authority-window, use-time-currency, or all"
        ),
    )
    verify_parser.add_argument(
        "--cluster",
        action="store_true",
        help=(
            "Run the optional two-worker cluster fault test and emit a signed "
            "deployment attestation; requires verify.cluster.enabled: true"
        ),
    )
    verify_parser.add_argument(
        "--attestation-output",
        type=Path,
        help="Also write the signed cluster attestation JSON to this file",
    )
    verify_parser.add_argument(
        "--verify-attestation",
        type=Path,
        metavar="FILE",
        help="Verify a previously produced deployment attestation instead of running tests",
    )
    verify_parser.add_argument(
        "--attestation-key-env",
        metavar="ENV",
        help="Environment variable containing the key for --verify-attestation",
    )
    verify_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit stable machine-readable JSON on stdout (diagnostics on stderr)",
    )
    verify_parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as failures (exit 1)",
    )
    verify_parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Per-scenario and subprocess timeout in seconds (default: 30)",
    )
    verify_parser.add_argument(
        "--rounds",
        type=int,
        default=5,
        help="Contention rounds (default: 5)",
    )
    verify_parser.add_argument(
        "--workers",
        type=int,
        default=2,
        help="Contention workers, 2-8 (default: 2)",
    )
    verify_parser.add_argument(
        "--keep-artifacts",
        action="store_true",
        help="Retain namespaced synthetic evidence instead of cleaning up",
    )
    verify_parser.add_argument(
        "--no-connectivity",
        action="store_true",
        help="Skip Doctor's backend connectivity probes",
    )

    init_parser = sub.add_parser(
        "init",
        help="Create mycelium.yaml in the current project",
        description=(
            "Scaffold mycelium.yaml for the wrapper path "
            "(mycelium run / @config.apply / @ledger_sync). Prefer wrappers. "
            "For a custom tool loop that needs explicit claim → execute → complete "
            "(PROCEED/SKIP-style), see the SDK README section "
            "'Manual integration (claim → execute → complete)' — there is no "
            "YAML switch; that API is called from your code."
        ),
    )
    init_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Output path (default: ./mycelium.yaml)",
    )
    init_mode = init_parser.add_mutually_exclusive_group()
    init_mode.add_argument(
        "--full",
        action="store_true",
        help="Reference template with all guards (not the default on-ramp)",
    )
    init_mode.add_argument(
        "--minimal",
        action="store_true",
        help="Smaller multi-guard scaffold (not the default on-ramp)",
    )
    init_mode.add_argument(
        "--detect",
        action="store_true",
        help="Inspect this project and create a conservative tailored starter",
    )
    init_parser.add_argument(
        "--project",
        type=Path,
        default=Path("."),
        help="Project directory scanned by --detect (default: current directory)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing file",
    )

    config_parser = sub.add_parser(
        "config",
        help="Inspect the versioned mycelium.yaml configuration contract",
    )
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_schema_parser = config_sub.add_parser(
        "schema",
        help="Print JSON Schema for IDEs, agents, and CI",
    )
    config_schema_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write the schema to a file instead of stdout",
    )
    config_docs_parser = config_sub.add_parser(
        "docs",
        help="Print Markdown reference generated from the typed model",
    )
    config_docs_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write Markdown to a file instead of stdout",
    )
    config_example_parser = config_sub.add_parser(
        "example",
        help="Print a model-validated example configuration",
    )
    config_example_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Write YAML to a file instead of stdout",
    )

    skills_parser = sub.add_parser(
        "skills",
        help="Install agent skills bundled with mycelium-runtime",
        description=(
            "Install bundled skills offline from the PyPI package. The default "
            "project catalog is ./.agents/skills; use --target for a user or "
            "agent-specific catalog such as ~/.codex/skills."
        ),
    )
    skills_sub = skills_parser.add_subparsers(dest="skills_command", required=True)
    skills_install_parser = skills_sub.add_parser(
        "install",
        help="Install the official mycelium-setup skill",
    )
    skills_install_parser.add_argument(
        "--target",
        type=Path,
        default=Path(".agents/skills"),
        metavar="CATALOG",
        help="Skill catalog directory (default: ./.agents/skills)",
    )
    skills_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace a different existing mycelium-setup skill",
    )

    demo_parser = sub.add_parser(
        "demo",
        help="Feature tour: without/with Mycelium + gates, repair, reconcile, release",
    )
    demo_parser.add_argument(
        "--redis",
        action="store_true",
        help=(
            "Also run the two-worker real-Redis Cloud-style redispatch proof "
            "(requires Redis; MYCELIUM_TEST_REDIS_URL or localhost db 15)"
        ),
    )
    demo_parser.add_argument(
        "--slow",
        action="store_true",
        help="Pause between lines/sections for screen recording (~30–40s total)",
    )
    run_parser = sub.add_parser(
        "run",
        help="Run a Python command with YAML callables auto-instrumented",
    )
    run_parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path (default: ./mycelium.yaml)",
    )
    run_parser.add_argument(
        "child_command",
        nargs=argparse.REMAINDER,
        help="Python command after '--'",
    )

    migrate_parser = sub.add_parser(
        "migrate",
        help="Plan or apply ActionLedger schema migrations",
        description=(
            "Upgrade durable ActionLedger rows using explicit version-to-version rules. "
            "Stop workers and back up the ledger before --apply."
        ),
    )
    _add_operator_storage_args(migrate_parser)
    migrate_mode = migrate_parser.add_mutually_exclusive_group(required=True)
    migrate_mode.add_argument(
        "--plan",
        action="store_true",
        help="Inspect versions and show changes without modifying ledger rows",
    )
    migrate_mode.add_argument(
        "--apply",
        action="store_true",
        help="Apply the migration (back up the ledger and stop workers first)",
    )
    migrate_parser.add_argument(
        "--target-version",
        type=int,
        default=LEDGER_ENTRY_SCHEMA_VERSION,
        help=(
            f"Target ledger schema version (default: current schema {LEDGER_ENTRY_SCHEMA_VERSION})"
        ),
    )
    migrate_parser.add_argument(
        "--allow-active",
        action="store_true",
        help="Allow IN_FLIGHT rows after workers are confirmed stopped",
    )
    migrate_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable migration plan or result",
    )

    state_parser = sub.add_parser(
        "state",
        help="Manage the unified guard state backend",
    )
    state_sub = state_parser.add_subparsers(dest="state_command", required=True)
    state_migrate_parser = state_sub.add_parser(
        "migrate",
        help="Copy legacy guard state into state_backend",
        description=(
            "Copy loop, scope, completion, state-flush, and audit-receipt records "
            "without deleting or overwriting their source records."
        ),
    )
    state_migrate_parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config containing source feature storage and destination state_backend",
    )
    state_migrate_mode = state_migrate_parser.add_mutually_exclusive_group(required=True)
    state_migrate_mode.add_argument(
        "--plan",
        action="store_true",
        help="Read-only preview of records to copy",
    )
    state_migrate_mode.add_argument(
        "--apply",
        action="store_true",
        help="Copy records without deleting source state",
    )
    state_migrate_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable output",
    )

    providers_parser = sub.add_parser(
        "providers",
        help="Verify shipped read-only provider reconciliation adapters",
    )
    providers_sub = providers_parser.add_subparsers(dest="providers_command", required=True)
    providers_verify_parser = providers_sub.add_parser(
        "verify",
        help="Run adversarial conformance tests and sign the report",
        description=(
            "Run synthetic lag, ambiguity, duplicate, malformed-handle, and "
            "forbidden-write checks. This does not contact the live provider."
        ),
    )
    providers_verify_parser.add_argument(
        "adapter",
        choices=("gmail",),
        help="Shipped provider adapter to verify",
    )
    providers_verify_parser.add_argument(
        "--signing-key-env",
        default=_ENV_ADAPTER_REPORT_SIGNING_KEY,
        help=(
            "Environment variable containing the HMAC signing key "
            f"(default: {_ENV_ADAPTER_REPORT_SIGNING_KEY})"
        ),
    )
    providers_verify_parser.add_argument(
        "--key-id",
        default=None,
        help="Non-secret signer key identifier stored in the report",
    )
    providers_verify_parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Write the signed JSON report to this path (default: stdout)",
    )

    providers_report_parser = providers_sub.add_parser(
        "verify-report",
        help="Verify a signed adapter report",
    )
    providers_report_parser.add_argument("report", type=Path)
    providers_report_parser.add_argument(
        "--signing-key-env",
        default=_ENV_ADAPTER_REPORT_SIGNING_KEY,
        help=(
            "Environment variable containing the HMAC verification key "
            f"(default: {_ENV_ADAPTER_REPORT_SIGNING_KEY})"
        ),
    )
    providers_report_parser.add_argument(
        "--json", action="store_true", help="Emit machine-readable verification"
    )

    transitions_parser = sub.add_parser(
        "transitions",
        help="Operator triage and release of stuck (hard-blocked) transitions",
    )
    transitions_sub = transitions_parser.add_subparsers(dest="transitions_command", required=True)

    list_parser = transitions_sub.add_parser(
        "list", help="List ledger transitions (optionally only stuck ones)"
    )
    _add_operator_storage_args(list_parser)
    list_parser.add_argument(
        "--stuck",
        action="store_true",
        help="Only transitions that need operator attention, with next-action hints",
    )
    list_parser.add_argument("--tool", default=None, help="Filter by tool name")
    list_parser.add_argument(
        "--outcome",
        choices=[item.value for item in TerminalOutcome],
        default=None,
        help="Filter by resolved terminal outcome",
    )
    list_parser.add_argument("--limit", type=int, default=None, help="Return one bounded page")
    list_parser.add_argument("--cursor", default=None, help="Opaque cursor from a prior page")
    list_parser.add_argument(
        "--parent",
        default=None,
        metavar="REQUEST_ID",
        help="Only children of this handoff parent request_id",
    )
    list_parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    export_parser = transitions_sub.add_parser(
        "export", help="Export transitions as sanitized newline-delimited JSON"
    )
    _add_operator_storage_args(export_parser)
    export_parser.add_argument("--output", required=True)
    export_parser.add_argument("--tool", default=None)
    export_parser.add_argument("--outcome", choices=[item.value for item in TerminalOutcome])
    export_parser.add_argument("--page-size", type=int, default=500)

    prune_parser = transitions_sub.add_parser(
        "prune", help="Apply the transition retention policy (dry-run by default)"
    )
    _add_operator_storage_args(prune_parser)
    prune_parser.add_argument(
        "--older-than",
        default=None,
        metavar="DURATION",
        help="Override retention age (for example 30d)",
    )
    prune_parser.add_argument(
        "--outcome",
        action="append",
        default=None,
        choices=[item.value for item in TerminalOutcome],
        help="Eligible stored outcome; repeat to select multiple",
    )
    prune_parser.add_argument(
        "--archive", default=None, help="Write candidates to NDJSON before deletion"
    )
    prune_parser.add_argument("--page-size", type=int, default=500)
    prune_mode = prune_parser.add_mutually_exclusive_group()
    prune_mode.add_argument("--dry-run", action="store_true", help="Preview only (the default)")
    prune_mode.add_argument(
        "--execute", action="store_true", help="Permanently delete eligible rows"
    )

    show_parser = transitions_sub.add_parser(
        "show", help="Show one transition with provider-verification fields"
    )
    _add_operator_storage_args(show_parser)
    show_parser.add_argument("request_id")

    release_parser = transitions_sub.add_parser(
        "release",
        help="Record a human verification releasing a hard-blocked transition",
    )
    _add_operator_storage_args(release_parser)
    release_parser.add_argument("request_id")
    release_parser.add_argument(
        "--verified",
        required=True,
        choices=["completed", "not-executed"],
        help="completed: effect happened (supply --result-json); "
        "not-executed: effect provably never ran (grants one re-execution)",
    )
    release_parser.add_argument(
        "--result-json",
        default=None,
        help="JSON result recorded for --verified completed",
    )
    release_parser.add_argument("--by", required=True, help="Operator identity (audit stamp)")
    release_parser.add_argument("--reason", required=True, help="Why the release is justified")

    mark_dead_parser = transitions_sub.add_parser(
        "mark-dead",
        help="Assert a worker is dead so reclaim can proceed",
    )
    _add_operator_storage_args(mark_dead_parser)
    mark_dead_parser.add_argument("request_id")
    mark_dead_parser.add_argument("--by", required=True, help="Operator identity (audit stamp)")
    mark_dead_parser.add_argument("--reason", required=True, help="Why the worker is believed dead")
    mark_dead_parser.add_argument(
        "--override-heartbeat",
        action="store_true",
        default=False,
        help="Skip liveness check (use only when operator has direct evidence "
        "of death — bypass may cause a duplicate effect if worker is alive)",
    )

    loops_parser = sub.add_parser(
        "loops",
        help="Operator triage and release of AF-003 loop-guard hard-blocks",
    )
    loops_sub = loops_parser.add_subparsers(dest="loops_command", required=True)

    loops_status = loops_sub.add_parser("status", help="Show loop-guard state for runs")
    loops_status.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Optional run_id / scope key (default: list all)",
    )
    loops_status.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path with loop_guard: (default: ./mycelium.yaml)",
    )
    loops_status.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Direct path to loop_guard JSON file storage",
    )
    loops_status.add_argument(
        "--stuck",
        action="store_true",
        help="Only hard-blocked runs",
    )
    loops_status.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    loops_release = loops_sub.add_parser(
        "release",
        help="Record a human verification releasing a loop-guard hard-block",
    )
    loops_release.add_argument("run_id")
    loops_release.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path with loop_guard: (default: ./mycelium.yaml)",
    )
    loops_release.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Direct path to loop_guard JSON file storage",
    )
    loops_release.add_argument(
        "--verified",
        required=True,
        choices=["clear", "allow-once", "abort-run"],
        help="clear: wipe counters; allow-once: one matching action; abort-run: keep frozen",
    )
    loops_release.add_argument("--by", required=True, help="Operator identity (audit stamp)")
    loops_release.add_argument("--reason", required=True, help="Why the release is justified")

    completion_parser = sub.add_parser(
        "completion",
        help="AF-007 completion contract: status and mark subtasks before terminal",
    )
    completion_sub = completion_parser.add_subparsers(dest="completion_command", required=True)

    completion_status = completion_sub.add_parser(
        "status", help="Show completion-contract checklist for runs"
    )
    completion_status.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Optional run_id / scope key (default: list all)",
    )
    completion_status.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path with completion: (default: ./mycelium.yaml)",
    )
    completion_status.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Direct path to completion JSON file storage",
    )
    completion_status.add_argument(
        "--required",
        default="",
        help="Comma-separated required ids (with --file, no config)",
    )
    completion_status.add_argument(
        "--optional",
        default="",
        help="Comma-separated optional ids (with --file, no config)",
    )
    completion_status.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output"
    )

    completion_mark = completion_sub.add_parser(
        "mark",
        help="Mark a subtask success|failed|abandoned for a run",
    )
    completion_mark.add_argument("run_id", help="run_id / scope key")
    completion_mark.add_argument("subtask_id", help="Checklist id to mark")
    completion_mark.add_argument(
        "--status",
        required=True,
        choices=["success", "failed", "abandoned"],
        help="Resolution status (abandoned requires --reason)",
    )
    completion_mark.add_argument(
        "--reason",
        default=None,
        help="Required when --status abandoned",
    )
    completion_mark.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path with completion: (default: ./mycelium.yaml)",
    )
    completion_mark.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Direct path to completion JSON file storage",
    )
    completion_mark.add_argument(
        "--required",
        default="",
        help="Comma-separated required ids (with --file, no config)",
    )
    completion_mark.add_argument(
        "--optional",
        default="",
        help="Comma-separated optional ids (with --file, no config)",
    )

    budget_parser = sub.add_parser(
        "budget",
        help="Budget guard: status and release for cost/time/step ceilings",
    )
    budget_sub = budget_parser.add_subparsers(dest="budget_command", required=True)

    budget_status = budget_sub.add_parser(
        "status", help="Show remaining budget and hard-block state for runs"
    )
    budget_status.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Optional run_id / scope key (default: list all)",
    )
    budget_status.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path with budget: (default: ./mycelium.yaml)",
    )
    budget_status.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Direct path to budget JSON file storage",
    )
    budget_status.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    budget_release = budget_sub.add_parser(
        "release",
        help="Operator release for a hard-blocked budget run",
    )
    budget_release.add_argument("run_id")
    budget_release.add_argument(
        "--verified",
        required=True,
        choices=["clear", "allow-once", "abort-run"],
        help="clear resets meters; allow-once permits one overage step; abort-run keeps blocked",
    )
    budget_release.add_argument(
        "--by",
        required=True,
        help="Operator identity (audit stamp, not authentication)",
    )
    budget_release.add_argument(
        "--reason",
        required=True,
        help="Why this overage / abort is authorized",
    )
    budget_release.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path with budget: (default: ./mycelium.yaml)",
    )
    budget_release.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Direct path to budget JSON file storage",
    )

    scope_parser = sub.add_parser(
        "scope",
        help="AF-008 scope-escalation guard: inspect or bind frozen allowlists",
    )
    scope_sub = scope_parser.add_subparsers(dest="scope_command", required=True)

    scope_status = scope_sub.add_parser("status", help="Show frozen tool allowlists for runs")
    scope_status.add_argument(
        "run_id",
        nargs="?",
        default=None,
        help="Optional run_id / thread_id scope key",
    )
    scope_status.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path with scope_guard: (default: ./mycelium.yaml)",
    )
    scope_status.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Direct path to scope-guard JSON file storage",
    )
    scope_status.add_argument(
        "--allowed-tools",
        default="",
        help="Comma-separated tools (with --file, optional default grant)",
    )
    scope_status.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    scope_bind = scope_sub.add_parser(
        "bind", help="Freeze (or narrow) a tool allowlist for a run_id"
    )
    scope_bind.add_argument("run_id", help="run_id / thread_id to freeze")
    scope_bind.add_argument(
        "--allowed-tools",
        default="",
        help="Comma-separated tool allowlist (default: config grant)",
    )
    scope_bind.add_argument(
        "-c",
        "--config",
        type=Path,
        default=Path("mycelium.yaml"),
        help="Config path with scope_guard: (default: ./mycelium.yaml)",
    )
    scope_bind.add_argument(
        "--file",
        type=Path,
        default=None,
        help="Direct path to scope-guard JSON file storage",
    )

    outcomes_parser = sub.add_parser(
        "outcomes",
        help="Outcome telemetry: compute DTTR over emitted resolution rows",
    )
    outcomes_sub = outcomes_parser.add_subparsers(dest="outcomes_command", required=True)

    dttr_parser = outcomes_sub.add_parser(
        "dttr",
        help="Compute the Duplicate Tool Transition Rate (DTTR) over an outcome log; target is 0.0",
    )
    dttr_parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="mycelium.yaml to read the outcome_emit storage from (default: ./mycelium.yaml)",
    )
    dttr_parser.add_argument(
        "--file",
        dest="outcome_file",
        type=Path,
        default=None,
        help=f"NDJSON outcome log path (or ${_ENV_OUTCOME_FILE}); overrides --config",
    )
    dttr_parser.add_argument(
        "--long-running-after",
        dest="long_running_after",
        type=float,
        default=None,
        help="seconds; transitions older than this count as long-running "
        "(default: outcome_emit.long_running_after, else disabled)",
    )
    dttr_parser.add_argument("--json", action="store_true", help="Machine-readable JSON output")

    return parser


def dispatch(args: argparse.Namespace) -> int:
    from mycelium.cli.commands import (
        cmd_budget_release,
        cmd_budget_status,
        cmd_completion_mark,
        cmd_completion_status,
        cmd_config_docs,
        cmd_config_example,
        cmd_config_schema,
        cmd_demo,
        cmd_doctor,
        cmd_init,
        cmd_coverage,
        cmd_preview,
        cmd_loops_release,
        cmd_loops_status,
        cmd_outcomes_dttr,
        cmd_providers_verify,
        cmd_providers_verify_report,
        cmd_run,
        cmd_scope_bind,
        cmd_scope_status,
        cmd_sidecar_serve,
        cmd_skills_install,
        cmd_verify,
    )
    from mycelium.cli_migrations import cmd_migrate, cmd_state_migrate
    from mycelium.cli_transitions import (
        cmd_transitions_export,
        cmd_transitions_list,
        cmd_transitions_mark_dead,
        cmd_transitions_prune,
        cmd_transitions_release,
        cmd_transitions_show,
    )

    if args.command == "sidecar":
        return cmd_sidecar_serve(args.config)
    if args.command == "init":
        return cmd_init(
            args.output,
            full=args.full,
            minimal=args.minimal,
            force=args.force,
            detect=args.detect,
            project=args.project,
        )
    if args.command == "demo":
        return cmd_demo(redis=args.redis, slow=args.slow)
    if args.command == "run":
        return cmd_run(args.config, args.child_command)
    if args.command == "migrate":
        return cmd_migrate(args)
    if args.command == "config":
        if args.config_command == "schema":
            return cmd_config_schema(args.output)
        if args.config_command == "docs":
            return cmd_config_docs(args.output)
        if args.config_command == "example":
            return cmd_config_example(args.output)
    if args.command == "skills":
        if args.skills_command == "install":
            return cmd_skills_install(target=args.target, force=args.force)
    if args.command == "state":
        if args.state_command == "migrate":
            return cmd_state_migrate(args)
    if args.command == "providers":
        if args.providers_command == "verify":
            return cmd_providers_verify(args)
        if args.providers_command == "verify-report":
            return cmd_providers_verify_report(args)
    if args.command == "transitions":
        if args.transitions_command == "list":
            return cmd_transitions_list(args)
        if args.transitions_command == "show":
            return cmd_transitions_show(args)
        if args.transitions_command == "export":
            return cmd_transitions_export(args)
        if args.transitions_command == "prune":
            return cmd_transitions_prune(args)
        if args.transitions_command == "release":
            return cmd_transitions_release(args)
        if args.transitions_command == "mark-dead":
            return cmd_transitions_mark_dead(args)
    if args.command == "loops":
        if args.loops_command == "status":
            return cmd_loops_status(args)
        if args.loops_command == "release":
            return cmd_loops_release(args)
    if args.command == "budget":
        if args.budget_command == "status":
            return cmd_budget_status(args)
        if args.budget_command == "release":
            return cmd_budget_release(args)
    if args.command == "completion":
        if args.completion_command == "status":
            return cmd_completion_status(args)
        if args.completion_command == "mark":
            return cmd_completion_mark(args)
    if args.command == "scope":
        if args.scope_command == "status":
            return cmd_scope_status(args)
        if args.scope_command == "bind":
            return cmd_scope_bind(args)
    if args.command == "outcomes":
        if args.outcomes_command == "dttr":
            return cmd_outcomes_dttr(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "coverage":
        return cmd_coverage(args)
    if args.command == "preview":
        return cmd_preview(args)
    if args.command == "verify":
        return cmd_verify(args)
    return 1


def run_cli(argv: list[str] | None = None) -> int:
    parser = build_parser()
    return dispatch(parser.parse_args(argv))
