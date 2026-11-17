#!/usr/bin/env python3
"""Plain-Python SQLite crash + operator-recovery walkthrough (#326).

Rehearses ambiguity resolution with durable local storage only: SQLite ledger,
independent fake-provider SQLite, and separate process invocations for the
worker, simulated crash, ambiguous redispatch, and operator recovery.

No Redis, LangGraph, credentials, or network.

Run the full demo from ``sdk/``::

    python examples/sqlite_crash_recovery/run.py

Or follow the step commands in README.md against a temporary workspace.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

# Checkout-friendly: import the local SDK without an install.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mycelium import (  # noqa: E402
    LedgerHardBlockError,
    LedgerWorkerAliveError,
    TerminalOutcome,
    TransitionScope,
    execution_scope,
)

from _lib import (  # noqa: E402
    ENV_WORKSPACE,
    LEASE_TTL,
    OPERATOR,
    RUN_ID,
    THREAD_ID,
    action_id_path,
    err_path,
    init_provider,
    ledger_path,
    make_charge,
    open_ledger,
    provider_effect_count,
    provider_lookup,
    provider_path,
    read_action_id,
    ready_path,
    require_workspace,
    write_config,
)


def cmd_init(root: Path, action_id: str | None) -> int:
    root.mkdir(parents=True, exist_ok=True)
    aid = action_id or f"charge-order:ORD-{uuid.uuid4().hex[:10]}"
    action_id_path(root).write_text(aid + "\n", encoding="utf-8")
    init_provider(root)
    write_config(root)
    for leftover in (ready_path(root), err_path(root)):
        if leftover.exists():
            leftover.unlink()
    print(f"[ok] init - workspace={root}")
    print(f"[ok] init - action_id={aid}")
    print(f"[ok] init - ledger={ledger_path(root)}")
    print(f"[ok] init - provider={provider_path(root)}")
    return 0


def cmd_worker(root: Path) -> int:
    """Claim, record the fake-provider effect, signal ready, then hang."""
    action_id = read_action_id(root)
    charge = make_charge(root, hang_after_effect=True, lease_renew_interval=0.0)
    try:
        with execution_scope(TransitionScope(thread_id=THREAD_ID, run_id=RUN_ID)):
            charge(amount=10.0, request_id=action_id)
    except Exception as exc:  # noqa: BLE001 - surface to parent via err file
        err_path(root).write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        raise
    return 0


def cmd_redispatch(root: Path) -> int:
    """Same host-owned action id in a new process; expect HARD_BLOCK or RETURN."""
    action_id = read_action_id(root)
    before = provider_effect_count(root)
    charge = make_charge(root, hang_after_effect=False, lease_ttl=30.0)
    outcome: str
    result: Any = None
    try:
        with execution_scope(TransitionScope(thread_id=THREAD_ID, run_id=RUN_ID)):
            result = charge(amount=10.0, request_id=action_id)
        outcome = "RETURN"
    except LedgerHardBlockError as exc:
        outcome = "HARD_BLOCK"
        result = str(exc).splitlines()[0]
    after = provider_effect_count(root)
    body_ran = after > before
    print(
        f"[ok] redispatch - outcome={outcome} "
        f"provider_effects={after} body_ran={body_ran} action_id={action_id}"
    )
    if outcome == "RETURN":
        print(f"[ok] redispatch - result={json.dumps(result, sort_keys=True)}")
    return 0


def cmd_mark_dead(root: Path) -> int:
    """Assert worker death on an EXPIRED row (lease age alone is never enough)."""
    action_id = read_action_id(root)
    ledger = open_ledger(root)
    entry = ledger.get(action_id)
    if entry is None:
        raise SystemExit(f"no ledger entry for {action_id!r}")
    outcome = entry.resolved_terminal_outcome()
    if outcome not in (TerminalOutcome.IN_FLIGHT, TerminalOutcome.EXPIRED):
        print(
            f"[ok] mark-dead - skipped (terminal={outcome.value}; "
            "already past EXPIRED / IN_FLIGHT)"
        )
        return 0
    ledger.mark_worker_dead_for(
        action_id,
        by=OPERATOR,
        reason="operator killed the worker process after the ready marker",
        override_heartbeat=True,
    )
    print(
        f"[ok] mark-dead - override_heartbeat=True action_id={action_id} "
        f"terminal={outcome.value}"
    )
    return 0


def cmd_recover(root: Path) -> int:
    """Operator path: inspect fake provider; record verified completed."""
    action_id = read_action_id(root)
    ledger = open_ledger(root)
    entry = ledger.get(action_id)
    if entry is None:
        raise SystemExit(f"no ledger entry for {action_id!r}")

    outcome = entry.resolved_terminal_outcome()
    print(
        f"[ok] recover - before action_id={action_id} "
        f"terminal={outcome.value} boundary={entry.side_effect_boundary} "
        f"ref={entry.external_operation_ref!r} "
        f"death_asserted={entry.worker_dead_asserted_at is not None}"
    )

    # If still EXPIRED without death evidence, assert death before release.
    if (
        outcome == TerminalOutcome.EXPIRED
        and entry.worker_dead_asserted_at is None
    ):
        ledger.mark_worker_dead_for(
            action_id,
            by=OPERATOR,
            reason="operator killed the worker process after the ready marker",
            override_heartbeat=True,
        )
        print("[ok] recover - mark_worker_dead override_heartbeat=True")
        entry = ledger.get(action_id) or entry

    op_ref = entry.external_operation_ref
    if not op_ref:
        raise SystemExit("missing external_operation_ref; cannot verify provider effect")
    receipt = provider_lookup(root, op_ref)
    if receipt is None:
        raise SystemExit(f"fake provider has no effect for {op_ref!r}")
    effects = provider_effect_count(root)
    print(
        f"[ok] recover - provider_inspect effects={effects} "
        f"receipt={json.dumps(receipt, sort_keys=True)}"
    )

    released = ledger.release(
        action_id,
        verified="completed",
        result=receipt,
        by=OPERATOR,
        reason=f"verified {op_ref} in local fake provider",
    )
    print(
        f"[ok] recover - release verified=completed "
        f"terminal={released.resolved_terminal_outcome().value} "
        f"provider_effects={effects}"
    )
    return 0


def cmd_status(root: Path) -> int:
    action_id = read_action_id(root)
    ledger = open_ledger(root)
    entry = ledger.get(action_id)
    effects = provider_effect_count(root)
    if entry is None:
        print(f"[ok] status - action_id={action_id} entry=missing provider_effects={effects}")
        return 0
    print(
        f"[ok] status - action_id={action_id} "
        f"terminal={entry.resolved_terminal_outcome().value} "
        f"boundary={entry.side_effect_boundary} "
        f"ref={entry.external_operation_ref!r} "
        f"provider_effects={effects} "
        f"operator_resolution={entry.operator_resolution!r}"
    )
    return 0


def _spawn_self(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env[ENV_WORKSPACE] = str(root)
    # Keep checkout import path for child processes.
    sdk_root = str(Path(__file__).resolve().parents[2])
    example_dir = str(Path(__file__).resolve().parent)
    env["PYTHONPATH"] = os.pathsep.join(
        [example_dir, sdk_root, env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    return subprocess.run(  # noqa: S603 - fixed interpreter, repo-owned script
        [sys.executable, str(Path(__file__).resolve()), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _print_child(label: str, completed: subprocess.CompletedProcess[str]) -> None:
    if completed.stdout:
        sys.stdout.write(completed.stdout)
        if not completed.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr or "")
        raise SystemExit(f"{label} failed with exit {completed.returncode}")


def cmd_demo(workspace: Path | None) -> int:
    """Orchestrate worker crash, ambiguous redispatch, and operator recovery."""
    created = workspace is None
    root = workspace or Path(tempfile.mkdtemp(prefix="mycelium-sqlite-crash-"))
    try:
        print("Mycelium walkthrough: plain-Python SQLite crash + operator recovery")
        print(f"  workspace={root}")
        print()

        _print_child("init", _spawn_self(root, "init"))
        action_id = read_action_id(root)

        # Separate worker invocation; parent simulates the crash with SIGKILL.
        env = os.environ.copy()
        env[ENV_WORKSPACE] = str(root)
        sdk_root = str(Path(__file__).resolve().parents[2])
        example_dir = str(Path(__file__).resolve().parent)
        env["PYTHONPATH"] = os.pathsep.join(
            [example_dir, sdk_root, env.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        proc = subprocess.Popen(  # noqa: S603
            [sys.executable, str(Path(__file__).resolve()), "worker"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 15.0
            while not ready_path(root).is_file():
                if time.time() >= deadline:
                    err = err_path(root).read_text(encoding="utf-8") if err_path(root).is_file() else ""
                    raise TimeoutError(f"worker never signaled ready ({err!r})")
                if proc.poll() is not None:
                    out, err = proc.communicate(timeout=2)
                    raise RuntimeError(
                        f"worker exited early code={proc.returncode} out={out!r} err={err!r}"
                    )
                time.sleep(0.02)

            effects_after_crash_window = provider_effect_count(root)
            if effects_after_crash_window != 1:
                raise AssertionError(
                    f"expected exactly one provider effect before kill, got {effects_after_crash_window}"
                )
            proc.kill()
            proc.wait(timeout=5)
            print(
                f"[ok] crash - killed worker after provider effect; "
                f"provider_effects={effects_after_crash_window} action_id={action_id}"
            )
            # Lease must lapse so redispatch sees EXPIRED, not POLL.
            time.sleep(LEASE_TTL + 0.5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)

        # Demonstrate: EXPIRED release without death evidence is refused.
        refuse = _spawn_self(root, "recover-refuse")
        _print_child("recover-refuse", refuse)

        # Supported death-signal path while the row is still EXPIRED.
        _print_child("mark-dead", _spawn_self(root, "mark-dead"))

        ambiguous = _spawn_self(root, "redispatch")
        _print_child("redispatch-ambiguous", ambiguous)
        if "outcome=HARD_BLOCK" not in ambiguous.stdout:
            raise AssertionError("expected HARD_BLOCK while outcome remains ambiguous")
        if "provider_effects=1" not in ambiguous.stdout or "body_ran=True" in ambiguous.stdout:
            raise AssertionError("ambiguous redispatch must not execute the provider again")

        _print_child("recover", _spawn_self(root, "recover"))

        settled = _spawn_self(root, "redispatch")
        _print_child("redispatch-settled", settled)
        if "outcome=RETURN" not in settled.stdout:
            raise AssertionError("expected RETURN after verified completed release")
        if "provider_effects=1" not in settled.stdout or "body_ran=True" in settled.stdout:
            raise AssertionError("settled redispatch must not execute the provider again")

        print()
        print(
            "[ok] walkthrough complete - provider_effects=1 "
            "ambiguous_hard_block=True verified_return=True"
        )
        print(
            "Proof: crash after fake-provider effect stays ambiguous until an "
            "operator verifies; lease age alone never authorizes another charge."
        )
        if created:
            print(f"Cleanup (temporary workspace only): rm -rf {root}")
        return 0
    except Exception:
        if created:
            print(f"Left workspace for inspection: {root}", file=sys.stderr)
        raise


def cmd_recover_refuse(root: Path) -> int:
    """Show EXPIRED release without death evidence fails (lease age is not enough)."""
    action_id = read_action_id(root)
    ledger = open_ledger(root)
    entry = ledger.get(action_id)
    if entry is None:
        raise SystemExit(f"no ledger entry for {action_id!r}")
    outcome = entry.resolved_terminal_outcome()
    if outcome != TerminalOutcome.EXPIRED:
        # Still useful when called after HARD_BLOCK: report current state.
        print(
            f"[ok] recover-refuse - skipped (terminal={outcome.value}; "
            "expected EXPIRED before ambiguous redispatch)"
        )
        return 0
    try:
        ledger.release(
            action_id,
            verified="completed",
            result={"should_not": "happen"},
            by=OPERATOR,
            reason="lease expired so it must be safe (incorrect)",
        )
    except LedgerWorkerAliveError as exc:
        print(
            f"[ok] recover-refuse - release_without_death refused: {type(exc).__name__} "
            f"(lease age alone is not proof of safety)"
        )
        return 0
    raise SystemExit("release without death evidence should have been refused")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plain-Python SQLite crash and operator-recovery walkthrough"
    )
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init", help="create ledger/provider files and stable action id")
    init_p.add_argument("--action-id", default=None, help="host-owned action id (default: generated)")

    sub.add_parser("worker", help="claim, record fake-provider effect, hang before complete")
    sub.add_parser("redispatch", help="same action id; HARD_BLOCK while ambiguous or RETURN after release")
    sub.add_parser(
        "recover-refuse",
        help="show EXPIRED release without death evidence is refused",
    )
    sub.add_parser(
        "mark-dead",
        help="assert worker death with override_heartbeat (EXPIRED / IN_FLIGHT only)",
    )
    sub.add_parser(
        "recover",
        help="inspect fake provider and release verified=completed",
    )
    sub.add_parser("status", help="print ledger + provider counts for the workspace")

    demo_p = sub.add_parser("demo", help="orchestrate the full walkthrough (default)")
    demo_p.add_argument(
        "--workspace",
        default=None,
        help="existing directory; otherwise create under tempfile and print cleanup",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["demo"]
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "demo"

    if command == "demo":
        workspace = Path(args.workspace).resolve() if args.workspace else None
        if workspace is not None:
            workspace.mkdir(parents=True, exist_ok=True)
            os.environ[ENV_WORKSPACE] = str(workspace)
        return cmd_demo(workspace)

    root = require_workspace()
    if command == "init":
        return cmd_init(root, args.action_id)
    if command == "worker":
        return cmd_worker(root)
    if command == "redispatch":
        return cmd_redispatch(root)
    if command == "recover-refuse":
        return cmd_recover_refuse(root)
    if command == "mark-dead":
        return cmd_mark_dead(root)
    if command == "recover":
        return cmd_recover(root)
    if command == "status":
        return cmd_status(root)
    parser.error(f"unknown command {command!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
