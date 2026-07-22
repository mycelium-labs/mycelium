"""CLI entrypoint: init, demo, and command auto-instrumentation."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from importlib import resources
from pathlib import Path

_TEMPLATE_QUICKSTART = "mycelium.quickstart.yaml"
_TEMPLATE_FULL = "mycelium.template.yaml"
_TEMPLATE_MINIMAL = "mycelium.minimal.yaml"


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
    return 0


def cmd_demo(*, redis: bool = False) -> int:
    from mycelium.quickstart import run_demo

    return run_demo(redis=redis)


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mycelium",
        description="Mycelium runtime: scaffold config and utilities",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init", help="Create mycelium.yaml in the current project")
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
        help="Show LangGraph duplicate-tool bug and the v1.3 transition fix",
    )
    demo_parser.add_argument(
        "--redis",
        action="store_true",
        help=(
            "Also run the two-worker real-Redis Cloud-style redispatch proof "
            "(requires Redis; MYCELIUM_TEST_REDIS_URL or localhost db 15)"
        ),
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

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.output, full=args.full, minimal=args.minimal, force=args.force)
    if args.command == "demo":
        return cmd_demo(redis=args.redis)
    if args.command == "run":
        return cmd_run(args.config, args.child_command)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
