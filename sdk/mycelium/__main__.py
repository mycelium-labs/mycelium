"""CLI entrypoint: ``mycelium init`` and ``mycelium demo``."""

from __future__ import annotations

import argparse
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
            "Next: install mycelium-runtime[langgraph], rename agent_id/subagent_task, "
            "then use @config.apply in code."
        )
        print("Try: mycelium demo")
    else:
        print("Next: edit tool/task names, then load_config(...) in your agent code.")
    return 0


def cmd_demo() -> int:
    from mycelium.quickstart import run_demo

    return run_demo()


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

    sub.add_parser(
        "demo",
        help="Show LangGraph duplicate-tool bug and the v1.3 transition fix",
    )

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.output, full=args.full, minimal=args.minimal, force=args.force)
    if args.command == "demo":
        return cmd_demo()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
