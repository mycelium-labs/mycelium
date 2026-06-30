"""CLI entrypoint: ``mycelium init`` scaffolds a config file in the user's project."""

from __future__ import annotations

import argparse
import sys
from importlib import resources
from pathlib import Path

_TEMPLATE_FULL = "mycelium.template.yaml"
_TEMPLATE_MINIMAL = "mycelium.minimal.yaml"


def _load_template(minimal: bool) -> str:
    filename = _TEMPLATE_MINIMAL if minimal else _TEMPLATE_FULL
    path = resources.files("mycelium") / "templates" / filename
    return path.read_text(encoding="utf-8")


def cmd_init(output: Path, *, minimal: bool, force: bool) -> int:
    if output.exists() and not force:
        print(f"error: {output} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    output.write_text(_load_template(minimal), encoding="utf-8")
    variant = "minimal" if minimal else "full"
    print(f"Wrote {output} ({variant} template)")
    print("Next: edit tool/task names, then load_config(...) in your agent code.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mycelium",
        description="Mycelium runtime — scaffold config and utilities",
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
        "--minimal",
        action="store_true",
        help="Use the smaller template (fewer commented examples)",
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing file",
    )

    args = parser.parse_args(argv)
    if args.command == "init":
        return cmd_init(args.output, minimal=args.minimal, force=args.force)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
