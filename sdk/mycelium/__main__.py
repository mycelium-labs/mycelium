"""CLI entrypoint: init, demo, run, transitions, loops, and outcomes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
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
_ENV_OUTCOME_FILE = "MYCELIUM_OUTCOME_FILE"


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


def cmd_init(output: Path, *, full: bool, minimal: bool, force: bool) -> int:
    if output.exists() and not force:
        print(f"error: {output} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    text, label = _load_template(full=full, minimal=minimal)
    output.write_text(text, encoding="utf-8")
    print(f"Wrote {output} ({label} template)")
    if label == "quickstart":
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


def cmd_demo(*, redis: bool = False, slow: bool = False) -> int:
    from mycelium.quickstart import run_demo

    return run_demo(redis=redis, slow=slow)


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
            "'mycelium run' requires the current Python interpreter; use "
            f"{sys.executable!r}"
        )
    forbidden = {"-E", "-I", "-S"}
    present = forbidden.intersection(command[1:])
    if present:
        flags = ", ".join(sorted(present))
        raise ValueError(
            f"Python flag(s) {flags} disable safe Mycelium startup instrumentation"
        )
    return [executable, *command[1:]]


def cmd_run(config_path: Path, command: list[str]) -> int:
    """Replace this process with an auto-instrumented Python command."""
    from mycelium.auto_instrumentation import AUTO_CONFIG_ENV, AUTO_ENABLED_ENV
    from mycelium.config import ConfigError, load_config

    resolved_config = config_path.resolve()
    try:
        config = load_config(resolved_config)
        config.auto_instrumentation_targets()
        child_command = _validated_python_command(command)
    except (ConfigError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    bootstrap_dir = (
        Path(__file__).resolve().parent
        / "auto_instrumentation"
        / "site_bootstrap"
    )
    env = dict(os.environ)
    current_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(bootstrap_dir)
        if not current_pythonpath
        else os.pathsep.join((str(bootstrap_dir), current_pythonpath))
    )
    env[AUTO_ENABLED_ENV] = "1"
    env[AUTO_CONFIG_ENV] = str(resolved_config)

    try:
        os.execvpe(child_command[0], child_command, env)
    except OSError as exc:
        print(f"error: cannot start {child_command[0]!r}: {exc}", file=sys.stderr)
        return 127
    return 127


def _add_operator_storage_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="mycelium.yaml to read ledger storage from (default: ./mycelium.yaml)",
    )
    parser.add_argument(
        "--file",
        dest="file_path",
        type=Path,
        default=None,
        help=f"JSON file ledger path (or ${_ENV_LEDGER_FILE}); overrides --config",
    )
    parser.add_argument(
        "--redis-url",
        default=None,
        help=f"Redis ledger URL (or ${_ENV_REDIS_URL}); overrides --config",
    )
    parser.add_argument(
        "--postgres-dsn",
        default=None,
        help=f"Postgres ledger DSN (or ${_ENV_POSTGRES_DSN}); overrides --config",
    )


def _operator_storage_configs(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Resolve normalized ledger-storage config dicts from flags/env or config.

    Direct flags (--file/--redis-url/--postgres-dsn, with env fallback) win
    over --config. Config mode reuses each tool's normalized ``ledger:``
    section, deduplicating tools that share one backend.
    """
    from mycelium.config import ConfigError, load_config

    file_path = args.file_path or os.environ.get(_ENV_LEDGER_FILE)
    redis_url = args.redis_url or os.environ.get(_ENV_REDIS_URL)
    postgres_dsn = args.postgres_dsn or os.environ.get(_ENV_POSTGRES_DSN)
    direct: list[dict[str, Any]] = []
    if file_path:
        direct.append({"storage": "file", "path": str(file_path)})
    if redis_url:
        direct.append({"storage": "redis", "url": redis_url})
    if postgres_dsn:
        direct.append({"storage": "postgres", "dsn": postgres_dsn})
    if direct:
        return direct

    config_path = args.config if args.config is not None else Path("mycelium.yaml")
    if not config_path.is_file():
        raise ConfigError(
            f"no ledger storage specified and config not found: {config_path} "
            "(pass --config, or --file/--redis-url/--postgres-dsn)"
        )
    config = load_config(config_path)
    raws: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tool in config.tools.values():
        if tool.ledger is None:
            continue
        key = json.dumps(tool.ledger, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        raws.append(tool.ledger)
    if not raws:
        raise ConfigError(f"{config_path} declares no tool ledger storage")
    return raws


def _operator_ledgers(args: argparse.Namespace) -> list[Any]:
    """Build ActionLedgers over the resolved operator storage backends."""
    from mycelium.action_ledger import ActionLedger
    from mycelium.config import ConfigError, MyceliumConfig

    ledgers: list[Any] = []
    for raw in _operator_storage_configs(args):
        storage_type = raw.get("storage", "memory")
        if storage_type == "memory":
            raise ConfigError(
                "ledger storage is 'memory', which lives inside the agent "
                "process — the CLI cannot reach it. Use the Python API "
                "(ActionLedger.list_transitions() / ActionLedger.release(...)) "
                "from a process sharing that storage, or point the CLI at a "
                "durable backend with --file/--redis-url/--postgres-dsn"
            )
        ledgers.append(ActionLedger(storage=MyceliumConfig._build_ledger_storage(raw)))
    return ledgers


def _find_operator_entry(args: argparse.Namespace, request_id: str) -> tuple[Any, Any]:
    """Return (ledger, entry) for request_id across the resolved backends."""
    from mycelium.config import ConfigError

    ledgers = _operator_ledgers(args)
    for ledger in ledgers:
        entry = ledger.get(request_id)
        if entry is not None:
            return ledger, entry
    raise ConfigError(f"no ledger entry found for request {request_id!r}")


def _format_age(age_seconds: float) -> str:
    if age_seconds < 0:
        age_seconds = 0
    if age_seconds < 60:
        return f"{int(age_seconds)}s"
    if age_seconds < 3600:
        return f"{int(age_seconds // 60)}m"
    if age_seconds < 86400:
        return f"{int(age_seconds // 3600)}h"
    return f"{int(age_seconds // 86400)}d"


def _format_ts(timestamp: float | None) -> str:
    if timestamp is None:
        return "-"
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def _next_action_hint(entry: Any, resolved: Any) -> str:
    from mycelium.transition import TerminalOutcome

    rid = entry.request_id
    if resolved in (
        TerminalOutcome.BLOCKED,
        TerminalOutcome.UNKNOWN,
        TerminalOutcome.FAILED_AFTER_EFFECT,
    ):
        ref = f" (ref {entry.external_operation_ref})" if entry.external_operation_ref else ""
        return (
            f"verify effect with provider{ref}, then: mycelium transitions release "
            f"{rid} --verified completed|not-executed --by ... --reason ..."
        )
    if resolved == TerminalOutcome.EXPIRED:
        if entry.worker_dead_asserted_at is None:
            return (
                f"worker died mid-flight; if reclaim_requires_death_signal is on, "
                f"mark dead first: mycelium transitions mark-dead {rid} --by ... --reason ...; "
                f"otherwise release {rid} --verified completed|not-executed"
            )
        return (
            f"worker died mid-flight; verify with provider, then release {rid} "
            "--verified completed|not-executed"
        )
    # Stuck IN_FLIGHT: old but lease still held/unbounded.
    return (
        f"lease still held; confirm the worker ({entry.owner}) is dead and the "
        f"effect never ran before releasing {rid}"
    )


def _entry_row(entry: Any, now: float) -> dict[str, Any]:
    resolved = entry.resolved_terminal_outcome(now=now)
    row = entry.to_dict()
    row["resolved_outcome"] = resolved.value
    row["age_seconds"] = max(0.0, now - entry.started_at)
    row["last_heartbeat_at"] = entry.last_heartbeat_at
    row["worker_dead_asserted_by"] = entry.worker_dead_asserted_by
    row["worker_dead_asserted_at"] = entry.worker_dead_asserted_at
    return row


def cmd_transitions_list(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError

    try:
        ledgers = _operator_ledgers(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    now = time.time()
    entries = []
    for ledger in ledgers:
        entries.extend(ledger.list_transitions(stuck=args.stuck, tool=args.tool))
    entries.sort(key=lambda entry: entry.started_at)
    if args.json:
        print(json.dumps([_entry_row(entry, now) for entry in entries], indent=2, default=str))
        return 0
    if not entries:
        print("no transitions found" + (" (stuck only)" if args.stuck else ""))
        return 0
    for entry in entries:
        resolved = entry.resolved_terminal_outcome(now=now)
        age = _format_age(now - entry.started_at)
        line = f"{entry.request_id}  {entry.tool}  {resolved.value}  age={age}"
        if entry.operator_resolution is not None:
            line += f"  [released: {entry.operator_resolution} by {entry.resolved_by}]"
        print(line)
        if args.stuck:
            print(f"    next: {_next_action_hint(entry, resolved)}")
    return 0


def cmd_transitions_show(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError

    try:
        _, entry = _find_operator_entry(args, args.request_id)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    resolved = entry.resolved_terminal_outcome()
    print(f"request_id: {entry.request_id}")
    print(f"tool: {entry.tool}")
    print(f"args: {json.dumps(entry.args, default=str)}")
    print(f"kwargs: {json.dumps(entry.kwargs, default=str)}")
    print(f"status: {entry.status}")
    print(f"resolved_outcome: {resolved.value}")
    print(f"side_effect_boundary: {entry.side_effect_boundary}")
    print(f"started_at: {_format_ts(entry.started_at)}")
    print(f"finished_at: {_format_ts(entry.finished_at)}")
    print(f"lease_until: {_format_ts(entry.lease_until)}")
    print(f"owner: {entry.owner or '-'}")
    print(f"idempotency_key: {entry.idempotency_key}")
    print(f"provider_idempotency_key: {entry.provider_idempotency_key or '-'}")
    pkey_first = entry.provider_key_first_attempt_at
    if pkey_first is not None:
        pkey_age = time.time() - pkey_first
        print(f"provider_key_first_attempt_at: {_format_ts(pkey_first)} (age={pkey_age:.1f}s)")
    else:
        print("provider_key_first_attempt_at: -")
    print(f"external_operation_ref: {entry.external_operation_ref or '-'}")
    print(f"error: {entry.error or '-'}")
    print(f"result: {json.dumps(entry.result, default=str)}")
    print(f"receipt_ref: {entry.receipt_ref or '-'}")
    print(f"operator_resolution: {entry.operator_resolution or '-'}")
    print(f"resolved_by: {entry.resolved_by or '-'}")
    print(f"resolution_reason: {entry.resolution_reason or '-'}")
    print(f"resolved_at: {_format_ts(entry.resolved_at)}")
    print(f"released_from_outcome: {entry.released_from_outcome or '-'}")
    print(f"last_heartbeat_at: {_format_ts(entry.last_heartbeat_at)}")
    print(f"worker_dead_asserted_by: {entry.worker_dead_asserted_by or '-'}")
    print(f"worker_dead_asserted_at: {_format_ts(entry.worker_dead_asserted_at)}")
    return 0


def cmd_transitions_release(args: argparse.Namespace) -> int:
    from mycelium.action_ledger import (
        OPERATOR_RESOLUTION_COMPLETED,
        OPERATOR_RESOLUTION_NOT_EXECUTED,
        LedgerError,
    )
    from mycelium.config import ConfigError

    verified = (
        OPERATOR_RESOLUTION_COMPLETED
        if args.verified == "completed"
        else OPERATOR_RESOLUTION_NOT_EXECUTED
    )
    result = None
    if args.result_json is not None:
        if verified != OPERATOR_RESOLUTION_COMPLETED:
            print(
                "error: --result-json only applies to --verified completed",
                file=sys.stderr,
            )
            return 2
        try:
            result = json.loads(args.result_json)
        except json.JSONDecodeError as exc:
            print(f"error: invalid --result-json: {exc}", file=sys.stderr)
            return 2
    try:
        ledger, _ = _find_operator_entry(args, args.request_id)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        entry = ledger.release(
            args.request_id,
            verified=verified,
            result=result,
            by=args.by,
            reason=args.reason,
        )
    except LedgerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"released {entry.request_id} ({entry.tool}): verified={verified} "
        f"by={args.by} from={entry.released_from_outcome}"
    )
    if verified == OPERATOR_RESOLUTION_COMPLETED:
        print("next redispatch returns the recorded result without re-executing")
    else:
        print("next agent redispatch re-executes exactly once (release consumed)")
    return 0


def cmd_transitions_mark_dead(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError

    try:
        ledger, _ = _find_operator_entry(args, args.request_id)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        entry = ledger.mark_worker_dead_for(
            args.request_id,
            by=args.by,
            reason=args.reason,
            override_heartbeat=args.override_heartbeat,
        )
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"marked worker dead for {entry.request_id} ({entry.tool}): "
        f"asserted_by={entry.worker_dead_asserted_by}"
    )
    return 0


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
        long_running_after=(
            float(long_running_after) if long_running_after is not None else None
        ),
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
        print("no long-running or redispatched transitions: DTTR is undefined "
              "(denominator forced to 1)")
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


def _completion_from_args(args: argparse.Namespace) -> Any:
    """Build a CompletionContract from --config / --file operator flags."""
    from mycelium.completion_contract import (
        CompletionContract,
        FileCompletionStorage,
    )
    from mycelium.config import ConfigError, load_config

    if getattr(args, "file", None):
        required = [
            s.strip()
            for s in (getattr(args, "required", None) or "").split(",")
            if s.strip()
        ]
        optional = [
            s.strip()
            for s in (getattr(args, "optional", None) or "").split(",")
            if s.strip()
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
        print(
            f"{state.scope_key}  required={state.required}  "
            f"optional={state.optional}{flag_s}"
        )
        for sid in state.required + [
            x for x in state.optional if x not in state.required
        ]:
            mark = state.marks.get(sid)
            if mark is None:
                kind = "required" if sid in state.required else "optional"
                print(f"  {sid}: pending ({kind})")
            else:
                reason = f" reason={mark.reason!r}" if mark.reason else ""
                print(f"  {sid}: {mark.status}{reason}")
        if pending_r:
            print(
                f"  → mark required ids then retry complete_run / END "
                f"(pending: {pending_r})"
            )
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mycelium",
        description="Mycelium runtime: scaffold config and utilities",
    )
    sub = parser.add_subparsers(dest="command", required=True)

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
    init_parser.add_argument(
        "--full",
        action="store_true",
        help="Reference template with all guards (not the default on-ramp)",
    )
    init_parser.add_argument(
        "--minimal",
        action="store_true",
        help="Smaller multi-guard scaffold (not the default on-ramp)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing file",
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

    transitions_parser = sub.add_parser(
        "transitions",
        help="Operator triage and release of stuck (hard-blocked) transitions",
    )
    transitions_sub = transitions_parser.add_subparsers(
        dest="transitions_command", required=True
    )

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
        "--json", action="store_true", help="Machine-readable JSON output"
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
    release_parser.add_argument(
        "--by", required=True, help="Operator identity (audit stamp)"
    )
    release_parser.add_argument(
        "--reason", required=True, help="Why the release is justified"
    )

    mark_dead_parser = transitions_sub.add_parser(
        "mark-dead",
        help="Assert a worker is dead so reclaim can proceed",
    )
    _add_operator_storage_args(mark_dead_parser)
    mark_dead_parser.add_argument("request_id")
    mark_dead_parser.add_argument(
        "--by", required=True, help="Operator identity (audit stamp)"
    )
    mark_dead_parser.add_argument(
        "--reason", required=True, help="Why the worker is believed dead"
    )
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

    loops_status = loops_sub.add_parser(
        "status", help="Show loop-guard state for runs"
    )
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
    loops_status.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output"
    )

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
        help="clear: wipe counters; allow-once: one matching action; "
        "abort-run: keep frozen",
    )
    loops_release.add_argument(
        "--by", required=True, help="Operator identity (audit stamp)"
    )
    loops_release.add_argument(
        "--reason", required=True, help="Why the release is justified"
    )

    completion_parser = sub.add_parser(
        "completion",
        help="AF-007 completion contract: status and mark subtasks before terminal",
    )
    completion_sub = completion_parser.add_subparsers(
        dest="completion_command", required=True
    )

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

    outcomes_parser = sub.add_parser(
        "outcomes",
        help="Outcome telemetry: compute DTTR over emitted resolution rows",
    )
    outcomes_sub = outcomes_parser.add_subparsers(
        dest="outcomes_command", required=True
    )

    dttr_parser = outcomes_sub.add_parser(
        "dttr",
        help="Compute the Duplicate Tool Transition Rate (DTTR) over an "
        "outcome log; target is 0.0",
    )
    dttr_parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="mycelium.yaml to read the outcome_emit storage from "
        "(default: ./mycelium.yaml)",
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
    dttr_parser.add_argument(
        "--json", action="store_true", help="Machine-readable JSON output"
    )

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.output, full=args.full, minimal=args.minimal, force=args.force)
    if args.command == "demo":
        return cmd_demo(redis=args.redis, slow=args.slow)
    if args.command == "run":
        return cmd_run(args.config, args.child_command)
    if args.command == "transitions":
        if args.transitions_command == "list":
            return cmd_transitions_list(args)
        if args.transitions_command == "show":
            return cmd_transitions_show(args)
        if args.transitions_command == "release":
            return cmd_transitions_release(args)
        if args.transitions_command == "mark-dead":
            return cmd_transitions_mark_dead(args)
    if args.command == "loops":
        if args.loops_command == "status":
            return cmd_loops_status(args)
        if args.loops_command == "release":
            return cmd_loops_release(args)
    if args.command == "completion":
        if args.completion_command == "status":
            return cmd_completion_status(args)
        if args.completion_command == "mark":
            return cmd_completion_mark(args)
    if args.command == "outcomes":
        if args.outcomes_command == "dttr":
            return cmd_outcomes_dttr(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
