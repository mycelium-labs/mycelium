"""ActionLedger operator CLI commands (kept separate from parser/dispatcher)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from mycelium.transition import TerminalOutcome

_ENV_LEDGER_FILE = "MYCELIUM_LEDGER_FILE"
_ENV_REDIS_URL = "MYCELIUM_REDIS_URL"
_ENV_POSTGRES_DSN = "MYCELIUM_POSTGRES_DSN"
_ENV_SQLITE_PATH = "MYCELIUM_SQLITE_PATH"

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
    parser.add_argument(
        "--sqlite",
        dest="sqlite_path",
        type=Path,
        default=None,
        help=f"SQLite ledger DB path (or ${_ENV_SQLITE_PATH}); overrides --config",
    )


def _operator_storage_configs(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Resolve normalized ledger-storage config dicts from flags/env or config.

    Direct flags (--file/--redis-url/--postgres-dsn/--sqlite, with env fallback)
    win over --config. Config mode reuses each tool's normalized ``ledger:``
    section, deduplicating tools that share one backend.
    """
    from mycelium.config import ConfigError, load_config

    file_path = args.file_path or os.environ.get(_ENV_LEDGER_FILE)
    redis_url = args.redis_url or os.environ.get(_ENV_REDIS_URL)
    postgres_dsn = args.postgres_dsn or os.environ.get(_ENV_POSTGRES_DSN)
    sqlite_path = args.sqlite_path or os.environ.get(_ENV_SQLITE_PATH)
    direct: list[dict[str, Any]] = []
    if file_path:
        direct.append({"storage": "file", "path": str(file_path)})
    if redis_url:
        direct.append({"storage": "redis", "url": redis_url})
    if postgres_dsn:
        direct.append({"storage": "postgres", "dsn": postgres_dsn})
    if sqlite_path:
        direct.append({"storage": "sqlite", "path": str(sqlite_path)})
    if direct:
        return direct

    config_path = args.config if args.config is not None else Path("mycelium.yaml")
    if not config_path.is_file():
        raise ConfigError(
            f"no ledger storage specified and config not found: {config_path} "
            "(pass --config, or --file/--redis-url/--postgres-dsn/--sqlite)"
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

    return [ActionLedger(storage=storage) for storage in _operator_storages(args)]


def _operator_storages(args: argparse.Namespace) -> list[Any]:
    """Build the resolved durable operator storage backends."""
    from mycelium.config import ConfigError, MyceliumConfig

    storages: list[Any] = []
    for raw in _operator_storage_configs(args):
        storage_type = raw.get("storage", "memory")
        if storage_type == "memory":
            raise ConfigError(
                "ledger storage is 'memory', which lives inside the agent "
                "process — the CLI cannot reach it. Use the Python API "
                "(ActionLedger.list_transitions() / ActionLedger.release(...)) "
                "from a process sharing that storage, or point the CLI at a "
                "durable backend with --file/--redis-url/--postgres-dsn/--sqlite"
            )
        storages.append(MyceliumConfig._build_ledger_storage(raw))
    return storages


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
    from mycelium.onboarding import execution_report

    row["onboarding_report"] = execution_report(entry, now=now)
    return row


def cmd_transitions_list(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError

    try:
        ledgers = _operator_ledgers(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    now = time.time()
    outcome = TerminalOutcome(args.outcome) if args.outcome is not None else None
    entries = []
    next_cursors: list[str] = []
    for ledger in ledgers:
        if args.limit is not None or args.cursor is not None:
            page = ledger.list_transitions_page(
                limit=args.limit or 100,
                cursor=args.cursor,
                stuck=args.stuck,
                tool=args.tool,
                outcome=outcome,
                parent_request_id=getattr(args, "parent", None),
            )
            entries.extend(page.entries)
            if page.next_cursor is not None:
                next_cursors.append(page.next_cursor)
        else:
            entries.extend(
                ledger.list_transitions(
                    stuck=args.stuck,
                    tool=args.tool,
                    outcome=outcome,
                    parent_request_id=getattr(args, "parent", None),
                )
            )
    entries.sort(key=lambda entry: entry.started_at)
    if args.json:
        from mycelium.secret_protection import sanitize_for_evidence

        rows = sanitize_for_evidence([_entry_row(entry, now) for entry in entries])
        payload: Any = rows
        if args.limit is not None or args.cursor is not None:
            payload = {
                "transitions": rows,
                "next_cursor": next_cursors[0] if len(next_cursors) == 1 else None,
            }
        print(json.dumps(payload, indent=2, default=str))
        return 0
    if not entries:
        print("no transitions found" + (" (stuck only)" if args.stuck else ""))
        return 0
    for entry in entries:
        resolved = entry.resolved_terminal_outcome(now=now)
        age = _format_age(now - entry.started_at)
        line = f"{entry.request_id}  {entry.tool}  {resolved.value}  age={age}"
        if entry.parent_request_id is not None:
            line += f"  parent={entry.parent_request_id}"
        if entry.operator_resolution is not None:
            line += f"  [released: {entry.operator_resolution} by {entry.resolved_by}]"
        print(line)
        if args.stuck:
            print(f"    next: {_next_action_hint(entry, resolved)}")
    return 0


def cmd_transitions_export(args: argparse.Namespace) -> int:
    from mycelium.config import ConfigError
    from mycelium.secret_protection import sanitize_for_evidence
    from mycelium.transition import TerminalOutcome

    try:
        ledgers = _operator_ledgers(args)
        outcome = TerminalOutcome(args.outcome) if args.outcome else None
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with output.open("w", encoding="utf-8") as handle:
            for ledger in ledgers:
                cursor: str | None = None
                while True:
                    page = ledger.list_transitions_page(
                        limit=args.page_size,
                        cursor=cursor,
                        tool=args.tool,
                        outcome=outcome,
                    )
                    for entry in page.entries:
                        row = sanitize_for_evidence(entry.to_dict())
                        handle.write(json.dumps(row, default=str, sort_keys=True) + "\n")
                        count += 1
                    cursor = page.next_cursor
                    if cursor is None:
                        break
    except (ConfigError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"exported {count} transitions to {output}")
    return 0


def cmd_transitions_prune(args: argparse.Namespace) -> int:
    from mycelium.budget_guard import parse_duration_seconds
    from mycelium.config import ConfigError
    from mycelium.secret_protection import sanitize_for_evidence
    from mycelium.transition import TerminalOutcome

    try:
        ledgers = _operator_ledgers(args)
        before = None
        if args.older_than is not None:
            before = time.time() - parse_duration_seconds(args.older_than)
        outcomes = (
            frozenset(TerminalOutcome(value) for value in args.outcome) if args.outcome else None
        )
        candidates: list[Any] = []
        plans: list[tuple[Any, list[Any]]] = []
        for ledger in ledgers:
            selected, _ = ledger.prune_transitions(
                before=before,
                outcomes=outcomes,
                dry_run=True,
                limit=args.page_size,
            )
            candidates.extend(selected)
            plans.append((ledger, selected))
        if args.archive is not None:
            archive = Path(args.archive)
            archive.parent.mkdir(parents=True, exist_ok=True)
            with archive.open("w", encoding="utf-8") as handle:
                for entry in candidates:
                    handle.write(
                        json.dumps(
                            sanitize_for_evidence(entry.to_dict()),
                            default=str,
                            sort_keys=True,
                        )
                        + "\n"
                    )
        deleted = 0
        if args.execute:
            for ledger, selected in plans:
                deleted += ledger.delete_transitions([entry.request_id for entry in selected])
        mode = "would prune" if not args.execute else "pruned"
        print(f"{mode} {len(candidates) if not args.execute else deleted} transitions")
        if args.archive is not None:
            print(f"archive: {args.archive}")
    except (ConfigError, OSError, ValueError, NotImplementedError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
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
    from mycelium.secret_protection import sanitize_for_evidence, sanitize_text

    print(f"args: {json.dumps(sanitize_for_evidence(entry.args), default=str)}")
    print(f"kwargs: {json.dumps(sanitize_for_evidence(entry.kwargs), default=str)}")
    print(f"status: {entry.status}")
    print(f"resolved_outcome: {resolved.value}")
    print(f"parent_request_id: {entry.parent_request_id or '-'}")
    print(f"handoff_id: {entry.handoff_id or '-'}")
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
    print(f"error: {sanitize_text(entry.error) if entry.error else '-'}")
    print(f"result: {json.dumps(sanitize_for_evidence(entry.result), default=str)}")
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

