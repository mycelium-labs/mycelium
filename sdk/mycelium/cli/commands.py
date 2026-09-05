"""CLI command handlers and operator workflows."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from importlib import resources
from pathlib import Path
from typing import Any

_TEMPLATE_QUICKSTART = "mycelium.quickstart.yaml"
_TEMPLATE_FULL = "mycelium.template.yaml"
_TEMPLATE_MINIMAL = "mycelium.minimal.yaml"

# Direct operator-storage flags fall back to these environment variables, so
# operator machines without the app's mycelium.yaml can still reach the ledger.
_ENV_LEDGER_FILE = "MYCELIUM_LEDGER_FILE"
_ENV_REDIS_URL = "MYCELIUM_REDIS_URL"
_ENV_POSTGRES_DSN = "MYCELIUM_POSTGRES_DSN"
_ENV_SQLITE_PATH = "MYCELIUM_SQLITE_PATH"
_ENV_OUTCOME_FILE = "MYCELIUM_OUTCOME_FILE"
_ENV_ADAPTER_REPORT_SIGNING_KEY = "MYCELIUM_ADAPTER_REPORT_SIGNING_KEY"

def _load_template(*, full: bool, minimal: bool) -> tuple[str, str]:
    if full:
        filename = _TEMPLATE_FULL
        label = "full"
    elif minimal:
        filename = _TEMPLATE_MINIMAL
        label = "minimal"
    else:
        filename = _TEMPLATE_QUICKSTART
        label = "quickstart"
    path = resources.files("mycelium") / "templates" / filename
    return path.read_text(encoding="utf-8"), label


def cmd_init(
    output: Path,
    *,
    full: bool,
    minimal: bool,
    force: bool,
    detect: bool = False,
    project: Path = Path("."),
) -> int:
    if output.exists() and not force:
        print(f"error: {output} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    if detect:
        from mycelium.config_detect import render_detected_config, write_schema_sidecar

        text, detection = render_detected_config(project)
        label = "detected"
    else:
        text, label = _load_template(full=full, minimal=minimal)
        detection = None
    output.write_text(text, encoding="utf-8")
    print(f"Wrote {output} ({label} template)")
    if detection is not None:
        schema_path = write_schema_sidecar(output, force=force)
        frameworks = ", ".join(detection.frameworks) or "none"
        print(
            f"Detected frameworks: {frameworks}; decorated tools: "
            f"{len(detection.tools)}; scanned Python files: {detection.scanned_files}."
        )
        if schema_path is not None:
            print(f"Wrote {schema_path} (IDE schema)")
        print("Review detected tools before production use; mutation was assumed for safety.")
    elif label == "quickstart":
        print(
            "Next: install mycelium-runtime[langgraph], fill the IDs/callable path, "
            "then use 'mycelium run -- python -m your_package.app'."
        )
        print("Try: mycelium demo")
    else:
        print("Next: edit tool/task names, then load_config(...) in your agent code.")
    print(
        "Prefer wrappers (mycelium run / @ledger_sync). For a custom tool loop, "
        "see sdk/README.md — Manual integration (claim → execute → complete)."
    )
    return 0


def cmd_config_schema(output: Path | None) -> int:
    """Print or write the JSON Schema for the current config version."""
    from mycelium.config import config_json_schema

    text = json.dumps(config_json_schema(), indent=2, sort_keys=True) + "\n"
    if output is None:
        print(text, end="")
    else:
        output.write_text(text, encoding="utf-8")
        print(f"Wrote {output}")
    return 0


def _write_or_print_config_artifact(text: str, output: Path | None) -> int:
    if output is None:
        print(text, end="")
    else:
        output.write_text(text, encoding="utf-8")
        print(f"Wrote {output}")
    return 0


def cmd_config_docs(output: Path | None) -> int:
    """Print or write reference Markdown generated from JSON Schema."""
    from mycelium.config_artifacts import render_config_reference

    return _write_or_print_config_artifact(render_config_reference(), output)


def cmd_config_example(output: Path | None) -> int:
    """Print or write a model-validated starter configuration."""
    from mycelium.config_artifacts import render_config_example

    return _write_or_print_config_artifact(render_config_example(), output)


def cmd_skills_install(*, target: Path, force: bool) -> int:
    """Install the bundled setup skill into an agent skill catalog."""
    from mycelium._internal.skill_installer import SkillInstallError, install_setup_skill

    try:
        result = install_setup_skill(target, force=force)
    except (OSError, SkillInstallError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if result.changed:
        print(f"Installed mycelium-setup skill: {result.destination}")
    else:
        print(f"mycelium-setup skill is already current: {result.destination}")
    return 0


def cmd_sidecar_serve(config: Path) -> int:
    """Run the explicitly configured development-only sidecar."""
    from mycelium.sidecar import SidecarConfig, serve_config

    try:
        sidecar_config = SidecarConfig.from_yaml(config)
        serve_config(sidecar_config)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_demo(*, redis: bool = False, slow: bool = False) -> int:
    from mycelium.quickstart import run_demo

    return run_demo(redis=redis, slow=slow)


def _adapter_report_signing_key(env_name: str) -> str:
    value = os.environ.get(env_name, "").strip()
    if not value:
        raise ValueError(f"environment variable {env_name!r} is not set")
    return value


def cmd_providers_verify(args: argparse.Namespace) -> int:
    """Run the synthetic provider suite and write a signed report."""

    from mycelium.provider_conformance import (
        adapter_report_json,
        create_adapter_verification_report,
    )
    from mycelium.providers import get_provider_conformance_fixture

    try:
        signing_key = _adapter_report_signing_key(args.signing_key_env)
        fixture = get_provider_conformance_fixture(args.adapter)
        report = create_adapter_verification_report(
            fixture,
            signing_key=signing_key,
            signer_key_id=args.key_id or args.signing_key_env,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    rendered = adapter_report_json(report) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote signed adapter report: {args.output}")
        print(f"Adapter {report.adapter_name}: {report.status}")
    return 0 if report.verified else 1


def cmd_providers_verify_report(args: argparse.Namespace) -> int:
    """Verify a stored adapter report's signature and passing status."""

    from mycelium.provider_conformance import (
        AdapterVerificationReport,
        adapter_report_is_verified,
        adapter_report_matches_fixture,
        verify_adapter_report_signature,
    )
    from mycelium.providers import get_provider_conformance_fixture

    try:
        signing_key = _adapter_report_signing_key(args.signing_key_env)
        report = AdapterVerificationReport.from_dict(
            json.loads(args.report.read_text(encoding="utf-8"))
        )
    except (KeyError, json.JSONDecodeError, OSError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    authentic = verify_adapter_report_signature(report, signing_key)
    try:
        fixture = get_provider_conformance_fixture(report.adapter_name)
    except ValueError:
        fixture = None
    source_matches = fixture is not None and adapter_report_matches_fixture(report, fixture)
    verified = fixture is not None and adapter_report_is_verified(
        report, signing_key, fixture=fixture
    )
    if args.json:
        print(
            json.dumps(
                {
                    "adapter": report.adapter_name,
                    "authentic": authentic,
                    "report_status": report.status,
                    "source_matches": source_matches,
                    "verified": verified,
                },
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            f"Adapter {report.adapter_name}: signature="
            f"{'valid' if authentic else 'invalid'} status={report.status} "
            f"source={'matches' if source_matches else 'changed'}"
        )
    return 0 if verified else 1


def _validated_python_command(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise ValueError("missing command after '--'")

    executable = shutil.which(command[0])
    if executable is None:
        raise ValueError(f"Python executable not found: {command[0]!r}")
    if Path(executable).resolve() != Path(sys.executable).resolve():
        raise ValueError(
            f"'mycelium run' requires the current Python interpreter; use {sys.executable!r}"
        )
    forbidden = {"-E", "-I", "-S"}
    present = forbidden.intersection(command[1:])
    if present:
        flags = ", ".join(sorted(present))
        raise ValueError(f"Python flag(s) {flags} disable safe Mycelium startup instrumentation")
    return [executable, *command[1:]]


def cmd_run(config_path: Path, command: list[str]) -> int:
    """Replace this process with an auto-instrumented Python command."""
    from mycelium.auto_instrumentation import AUTO_CONFIG_ENV, AUTO_ENABLED_ENV
    from mycelium.config import ConfigError, _load_config_for_preflight

    resolved_config = config_path.resolve()
    try:
        # Validate structure and instrumentation targets without activating
        # application-owned runtime adapters in the launcher process. The
        # child startup hook activates them after its import path is ready.
        config = _load_config_for_preflight(resolved_config)
        config.auto_instrumentation_targets()
        child_command = _validated_python_command(command)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    bootstrap_dir = (
        Path(__file__).resolve().parents[1] / "auto_instrumentation" / "site_bootstrap"
    )
    env = dict(os.environ)
    # sitecustomize imports tools before the interpreter prepends CWD to
    # sys.path (``python -m`` / ``-c``). Keep CWD importable for fresh
    # project layouts like ``my_app.tools`` without requiring PYTHONPATH=.
    pythonpath_parts = [str(bootstrap_dir), os.getcwd()]
    current_pythonpath = env.get("PYTHONPATH")
    if current_pythonpath:
        pythonpath_parts.append(current_pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env[AUTO_ENABLED_ENV] = "1"
    env[AUTO_CONFIG_ENV] = str(resolved_config)

    try:
        os.execvpe(child_command[0], child_command, env)  # nosec B606  # execvpe replaces process with validated child command
    except OSError as exc:
        print(f"error: cannot start {child_command[0]!r}: {exc}", file=sys.stderr)
        return 127
    return 127


def _outcome_storage_config(
    args: argparse.Namespace,
) -> tuple[dict[str, Any], float | None]:
    """Resolve the outcome-log storage config + long_running_after override."""
    from mycelium.config import ConfigError, load_config

    file_path = args.outcome_file or os.environ.get(_ENV_OUTCOME_FILE)
    if file_path:
        return {"storage": "file", "path": str(file_path)}, None

    config_path = args.config if args.config is not None else Path("mycelium.yaml")
    if not config_path.is_file():
        raise ConfigError(
            f"no outcome log specified and config not found: {config_path} "
            "(pass --file, or --config with an 'outcome_emit' section)"
        )
    config = load_config(config_path)
    if config.outcome_emit is None:
        raise ConfigError(
            f"{config_path} declares no 'outcome_emit' section; "
            "add one (storage: file, path: ...) or pass --file"
        )
    long_running_after = config.outcome_emit.get("long_running_after")
    return config.outcome_emit, long_running_after


def cmd_outcomes_dttr(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError, MyceliumConfig
    from mycelium.outcome_emit import compute_dttr

    try:
        raw, config_long_running = _outcome_storage_config(args)
        storage = MyceliumConfig._build_outcome_storage(raw)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    long_running_after = args.long_running_after
    if long_running_after is None:
        long_running_after = config_long_running
    report = compute_dttr(
        storage.list_all(),
        long_running_after=(float(long_running_after) if long_running_after is not None else None),
    )
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
        return 0
    print(f"DTTR: {report.dttr:.4f}  (target: 0.0)")
    print(
        f"silent duplicates: {report.silent_duplicates}  "
        f"long-running or redispatched: {report.long_running_or_redispatched}  "
        f"transitions: {report.transitions}"
    )
    for item in report.per_transition:
        marker = " *" if item.long_running_or_redispatched else ""
        print(
            f"  {item.request_id}  {item.tool}  "
            f"execs={item.body_executions}  silent={item.silent_duplicates}  "
            f"resolutions={item.resolution_events}  dur={item.duration_seconds:.1f}s"
            f"{marker}"
        )
    if report.long_running_or_redispatched == 0 and report.transitions > 0:
        print(
            "no long-running or redispatched transitions: DTTR is undefined "
            "(denominator forced to 1)"
        )
    return 0


def _loop_guard_from_args(args: argparse.Namespace) -> Any:
    """Build a LoopGuard from --config / --file operator flags."""
    from mycelium.config import ConfigError, load_config
    from mycelium.loop_guard import FileLoopGuardStorage, LoopGuard

    if getattr(args, "file", None):
        return LoopGuard(FileLoopGuardStorage(args.file))

    config_path = Path(args.config) if args.config else Path("mycelium.yaml")
    if not config_path.is_file():
        raise ConfigError(
            f"no loop_guard storage specified and config not found: {config_path} "
            "(pass --config or --file)"
        )
    config = load_config(config_path)
    if config.loop_guard is None:
        raise ConfigError(f"{config_path} declares no loop_guard section")
    guard = config.build_loop_guard()
    if guard is None:
        raise ConfigError(f"{config_path} declares no loop_guard section")
    storage_type = config.loop_guard.get("storage", "memory")
    if storage_type == "memory":
        raise ConfigError(
            "loop_guard storage is 'memory', which lives inside the agent "
            "process — the CLI cannot reach it. Use --file or configure "
            "loop_guard.storage: file"
        )
    return guard


def _scope_guard_from_args(args: argparse.Namespace) -> Any:
    """Build a ScopeGuard from --config / --file operator flags."""
    from mycelium.config import ConfigError, load_config
    from mycelium.scope_guard import FileScopeGuardStorage, ScopeGrant, ScopeGuard

    if getattr(args, "file", None):
        allowed = [
            s.strip() for s in (getattr(args, "allowed_tools", None) or "").split(",") if s.strip()
        ]
        grant = ScopeGrant(allowed_tools=frozenset(allowed)) if allowed else None
        return ScopeGuard(FileScopeGuardStorage(args.file), default_grant=grant)

    config_path = Path(args.config) if args.config else Path("mycelium.yaml")
    if not config_path.is_file():
        raise ConfigError(
            f"no scope_guard storage specified and config not found: {config_path} "
            "(pass --config or --file)"
        )
    config = load_config(config_path)
    if config.scope_guard is None:
        raise ConfigError(f"{config_path} declares no scope_guard section")
    guard = config.build_scope_guard()
    if guard is None:
        raise ConfigError(f"{config_path} declares no scope_guard section")
    storage_type = config.scope_guard.get("storage", "memory")
    if storage_type == "memory":
        raise ConfigError(
            "scope_guard storage is 'memory', which lives inside the agent "
            "process — the CLI cannot reach it. Use --file or configure "
            "scope_guard.storage: file"
        )
    return guard


def cmd_scope_status(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError

    try:
        guard = _scope_guard_from_args(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.run_id:
        state = guard.get_state(args.run_id)
        states = [state] if state is not None else []
    else:
        states = guard.storage.list_all()

    if args.json:
        print(json.dumps([s.to_dict() for s in states], indent=2, default=str))
        return 0

    if not states:
        print("(no scope-guard frozen grants)")
        return 0

    for state in states:
        print(f"{state.scope_key}  tools={sorted(state.grant.allowed_tools)}")
    return 0


def cmd_scope_bind(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError
    from mycelium.scope_guard import ScopeGrant, ScopeWidenRefusedError

    try:
        guard = _scope_guard_from_args(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    allowed = [s.strip() for s in (args.allowed_tools or "").split(",") if s.strip()]
    if allowed:
        grant = ScopeGrant(allowed_tools=frozenset(allowed))
    elif guard.default_grant is not None:
        grant = guard.default_grant
    else:
        print(
            "error: pass --allowed-tools or configure scope_guard.allowed_tools",
            file=sys.stderr,
        )
        return 1

    try:
        state = guard.bind(args.run_id, grant)
    except ScopeWidenRefusedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"bound {state.scope_key}: tools={sorted(state.grant.allowed_tools)}")
    return 0


def _completion_from_args(args: argparse.Namespace) -> Any:
    """Build a CompletionContract from --config / --file operator flags."""
    from mycelium.completion_contract import (
        CompletionContract,
        FileCompletionStorage,
    )
    from mycelium.config import ConfigError, load_config

    if getattr(args, "file", None):
        required = [
            s.strip() for s in (getattr(args, "required", None) or "").split(",") if s.strip()
        ]
        optional = [
            s.strip() for s in (getattr(args, "optional", None) or "").split(",") if s.strip()
        ]
        if not required and not optional:
            raise ConfigError(
                "when using --file without a config, pass --required "
                "and/or --optional (comma-separated ids)"
            )
        return CompletionContract(
            FileCompletionStorage(args.file),
            required=required,
            optional=optional,
        )

    config_path = Path(args.config) if args.config else Path("mycelium.yaml")
    if not config_path.is_file():
        raise ConfigError(
            f"no completion storage specified and config not found: {config_path} "
            "(pass --config or --file)"
        )
    config = load_config(config_path)
    if config.completion is None:
        raise ConfigError(f"{config_path} declares no completion section")
    contract = config.build_completion_contract()
    if contract is None:
        raise ConfigError(f"{config_path} declares no completion section")
    storage_type = config.completion.get("storage", "memory")
    if storage_type == "memory":
        raise ConfigError(
            "completion storage is 'memory', which lives inside the agent "
            "process — the CLI cannot reach it. Use --file or configure "
            "completion.storage: file"
        )
    return contract


def cmd_completion_status(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError

    try:
        contract = _completion_from_args(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.run_id:
        state = contract.get_state(args.run_id)
        states = [state] if state is not None else []
        if not states:
            # Bind template so status shows pending checklist for a new run id.
            state = contract.bind_run(args.run_id)
            states = [state]
    else:
        states = contract.storage.list_all()

    if args.json:
        print(json.dumps([s.to_dict() for s in states], indent=2, default=str))
        return 0

    if not states:
        print("(no completion-contract runs)")
        return 0

    for state in states:
        pending_r = state.pending_required()
        pending_o = state.pending_optional()
        flags = []
        if pending_r:
            flags.append("REFUSE_IF_TERMINAL")
        elif pending_o:
            flags.append("WARN_OPTIONAL")
        else:
            flags.append("ALLOW")
        flag_s = f" [{' '.join(flags)}]" if flags else ""
        print(f"{state.scope_key}  required={state.required}  optional={state.optional}{flag_s}")
        for sid in state.required + [x for x in state.optional if x not in state.required]:
            mark = state.marks.get(sid)
            if mark is None:
                kind = "required" if sid in state.required else "optional"
                print(f"  {sid}: pending ({kind})")
            else:
                reason = f" reason={mark.reason!r}" if mark.reason else ""
                print(f"  {sid}: {mark.status}{reason}")
        if pending_r:
            print(f"  → mark required ids then retry complete_run / END (pending: {pending_r})")
    return 0


def cmd_completion_mark(args: argparse.Namespace) -> int:
    from mycelium.completion_contract import CompletionMarkError
    from mycelium.config import ConfigError

    try:
        contract = _completion_from_args(args)
        state = contract.mark(
            args.subtask_id,
            args.status,
            reason=args.reason,
            scope_key=args.run_id,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except CompletionMarkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mark = state.marks[args.subtask_id]
    print(
        f"marked {args.subtask_id}={mark.status} on run {state.scope_key}"
        + (f" reason={mark.reason!r}" if mark.reason else "")
    )
    return 0


def cmd_loops_status(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError

    try:
        guard = _loop_guard_from_args(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.run_id:
        state = guard.get_state(args.run_id)
        states = [state] if state is not None else []
        if not states:
            print(f"no loop-guard state for run {args.run_id!r}", file=sys.stderr)
            return 1
    else:
        states = guard.storage.list_all()
        if args.stuck:
            states = [s for s in states if s.hard_blocked]

    if args.json:
        print(json.dumps([s.to_dict() for s in states], indent=2, default=str))
        return 0

    if not states:
        print("(no loop-guard runs)")
        return 0

    for state in states:
        flags = []
        if state.hard_blocked:
            flags.append("HARD")
        if state.allow_once_hash:
            flags.append("ALLOW_ONCE")
        if state.operator_resolution:
            flags.append(f"released:{state.operator_resolution}")
        flag_s = f" [{' '.join(flags)}]" if flags else ""
        print(
            f"{state.scope_key}  streak={state.streak}  "
            f"last_hash={(state.last_hash or '-')[:12]}  "
            f"soft={len(state.soft_issued)}{flag_s}"
        )
        if state.hard_blocked and state.operator_resolution is None:
            print(
                f"  → mycelium loops release {state.scope_key} "
                f"--verified clear|allow-once|abort-run --by … --reason …"
            )
    return 0


def cmd_loops_release(args: argparse.Namespace) -> int:
    from mycelium.action_ledger import (
        LedgerAlreadyResolvedError,
        LedgerReleaseRefusedError,
    )
    from mycelium.config import ConfigError

    try:
        guard = _loop_guard_from_args(args)
        state = guard.release(
            args.run_id,
            verified=args.verified,
            by=args.by,
            reason=args.reason,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (LedgerReleaseRefusedError, LedgerAlreadyResolvedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(
        f"released run {state.scope_key} --verified {state.operator_resolution} "
        f"by {state.resolved_by}"
    )
    return 0


def _budget_guard_from_args(args: argparse.Namespace) -> Any:
    """Build a BudgetGuard from --config / --file operator flags."""
    from mycelium.budget_guard import BudgetGuard, FileBudgetGuardStorage
    from mycelium.config import ConfigError, load_config

    if getattr(args, "file", None):
        return BudgetGuard(
            FileBudgetGuardStorage(args.file),
            max_steps=10**9,  # CLI inspect/release only; ceilings unused
        )

    config_path = Path(args.config) if args.config else Path("mycelium.yaml")
    if not config_path.is_file():
        raise ConfigError(
            f"no budget storage specified and config not found: {config_path} "
            "(pass --config or --file)"
        )
    config = load_config(config_path)
    if config.budget is None:
        raise ConfigError(f"{config_path} declares no budget section")
    guard = config.build_budget_guard()
    if guard is None:
        raise ConfigError(f"{config_path} declares no budget section")
    storage_type = config.budget.get("storage", "memory")
    if storage_type == "memory":
        raise ConfigError(
            "budget storage is 'memory', which lives inside the agent "
            "process — the CLI cannot reach it. Use --file or configure "
            "budget.storage: file|sqlite|redis|postgres"
        )
    return guard


def cmd_budget_status(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError

    try:
        guard = _budget_guard_from_args(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.run_id:
        state = guard.get_state(args.run_id)
        states = [state] if state is not None else []
    else:
        states = guard.storage.list_all()

    if args.json:
        payload = []
        for state in states:
            row = state.to_dict()
            row["remaining_budget"] = state.remaining(guard.ceilings).to_dict()
            payload.append(row)
        print(json.dumps(payload, indent=2, default=str))
        return 0

    if not states:
        print("(no budget-guard runs)")
        return 0
    for state in states:
        remaining = state.remaining(guard.ceilings)
        print(
            f"{state.scope_key}  steps={state.steps}  tokens={state.tokens}  "
            f"usd={state.usd:.4f}  hard_blocked={state.hard_blocked}  "
            f"blocked={state.blocked_dimension!r}"
        )
        print(
            f"  remaining_budget: duration={remaining.duration_seconds}  "
            f"steps={remaining.steps}  tokens={remaining.tokens}  "
            f"usd={remaining.usd}"
        )
        if state.hard_blocked and state.operator_resolution is None:
            print(
                f"  → mycelium budget release {state.scope_key} "
                f"--verified clear|allow-once|abort-run --by … --reason …"
            )
    return 0


def cmd_budget_release(args: argparse.Namespace) -> int:
    from mycelium.action_ledger import (
        LedgerAlreadyResolvedError,
        LedgerReleaseRefusedError,
    )
    from mycelium.config import ConfigError

    try:
        guard = _budget_guard_from_args(args)
        state = guard.release(
            args.run_id,
            verified=args.verified,
            by=args.by,
            reason=args.reason,
        )
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (LedgerReleaseRefusedError, LedgerAlreadyResolvedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"released run {state.scope_key} --verified {state.operator_resolution} "
        f"by {state.resolved_by}"
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Production-safety verification with narrowly scoped opt-in fixes."""
    import sys

    from mycelium.doctor import exit_code_for_report, run_doctor
    from mycelium.doctor.render import write_report

    if args.fix:
        from mycelium.doctor.fixes import apply_conservative_fixes

        for fix in apply_conservative_fixes(args.config):
            print(f"fixed [{fix.id}] {fix.summary}: {fix.path}", file=sys.stderr)

    report = run_doctor(
        args.config,
        connectivity=not args.no_connectivity,
        timeout_seconds=float(args.timeout),
        verbose=bool(args.verbose),
    )
    write_report(
        report,
        as_json=bool(args.json),
        verbose=bool(args.verbose),
        stream=sys.stdout,
    )
    return exit_code_for_report(report, strict=bool(args.strict))


def cmd_verify(args: argparse.Namespace) -> int:
    """Empirical verification, with an explicitly optional cluster mode."""
    import sys

    if args.verify_attestation:
        from mycelium.verify.cluster import (
            DeploymentAttestation,
            deployment_attestation_is_verified,
        )

        if args.cluster or args.scenario:
            print(
                "error: --verify-attestation cannot be combined with --cluster or --scenario",
                file=sys.stderr,
            )
            return 2
        key_env = str(args.attestation_key_env or "")
        signing_key = os.environ.get(key_env, "") if key_env else ""
        try:
            if not signing_key:
                raise ValueError("--attestation-key-env must name a non-empty environment variable")
            attestation = DeploymentAttestation.from_dict(
                json.loads(args.verify_attestation.read_text(encoding="utf-8"))
            )
            valid = deployment_attestation_is_verified(attestation, signing_key)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
            print(f"error: invalid deployment attestation: {exc}", file=sys.stderr)
            return 2
        payload = {
            "attestation_id": attestation.attestation_id,
            "status": attestation.status,
            "signature_valid": valid,
            "signer_key_id": attestation.signer_key_id,
        }
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else payload)
        return 0 if valid else 1

    if bool(args.cluster):
        from mycelium.verify.cluster import cluster_exit_code, run_cluster_verify

        if args.scenario:
            print("error: --cluster cannot be combined with --scenario", file=sys.stderr)
            return 2
        result = run_cluster_verify(
            args.config,
            timeout_seconds=float(args.timeout),
            connectivity=not args.no_connectivity,
            keep_artifacts=bool(args.keep_artifacts),
        )
        rendered = json.dumps(result.to_dict(), indent=2, sort_keys=True)
        if args.json:
            print(rendered)
        else:
            print(f"Cluster verification: {result.status}")
            if result.attestation is not None:
                print(f"Attestation: {result.attestation.attestation_id}")
                for check in result.attestation.checks:
                    print(f"  [{check.status}] {check.name}: {check.detail}")
            if result.error:
                print(f"Error: {result.error}", file=sys.stderr)
        if args.attestation_output:
            if result.attestation is None:
                print("error: no attestation was produced", file=sys.stderr)
            else:
                try:
                    args.attestation_output.write_text(
                        json.dumps(result.attestation.to_dict(), indent=2, sort_keys=True) + "\n",
                        encoding="utf-8",
                    )
                except OSError as exc:
                    print(f"error: could not write attestation: {exc}", file=sys.stderr)
                    return 2
        return cluster_exit_code(result)

    from mycelium.verify import exit_code_for_verify, run_verify
    from mycelium.verify.render import write_report

    selected = list(args.scenario or [])
    if not selected:
        print("error: --scenario is required (repeatable, or 'all')", file=sys.stderr)
        return 2
    report = run_verify(
        args.config,
        scenarios=selected,
        timeout_seconds=float(args.timeout),
        rounds=int(args.rounds),
        workers=int(args.workers),
        keep_artifacts=bool(args.keep_artifacts),
        connectivity=not args.no_connectivity,
    )
    write_report(report, as_json=bool(args.json), stream=sys.stdout)
    if args.json and report.isolation_detail and report.refused:
        print(report.isolation_detail, file=sys.stderr)
    return exit_code_for_verify(report, strict=bool(args.strict))
