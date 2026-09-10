"""Exercise offline setup-skill installation from an installed distribution."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_CANONICAL = Path(__file__).parents[2] / ".agents/skills/mycelium-setup"


def _files(root: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(root): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    expected: int,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != expected:
        raise SystemExit(
            f"expected exit {expected}, got {result.returncode}: {' '.join(command)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--canonical", type=Path, default=DEFAULT_CANONICAL)
    args = parser.parse_args()

    canonical = _files(args.canonical)
    if not canonical:
        raise SystemExit(f"canonical skill is empty or missing: {args.canonical}")

    executable = Path(sys.executable).with_name("mycelium")
    if not executable.is_file():
        raise SystemExit(f"mycelium console script is missing: {executable}")

    with tempfile.TemporaryDirectory(prefix="mycelium-installed-skill-") as temporary:
        root = Path(temporary)
        project = root / "project"
        catalog = project / ".agents/skills"
        destination = catalog / "mycelium-setup"
        offline = root / "offline"
        sentinel = root / "offline-hook-loaded"
        project.mkdir()
        offline.mkdir()
        (offline / "sitecustomize.py").write_text(
            "import socket\n"
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('loaded', encoding='utf-8')\n"
            "def deny(*args, **kwargs):\n"
            "    raise RuntimeError('network access attempted during offline skill install')\n"
            "class OfflineSocket(socket.socket):\n"
            "    def connect(self, *args, **kwargs):\n"
            "        return deny(*args, **kwargs)\n"
            "    def connect_ex(self, *args, **kwargs):\n"
            "        return deny(*args, **kwargs)\n"
            "socket.socket = OfflineSocket\n"
            "socket.create_connection = deny\n"
            "socket.getaddrinfo = deny\n",
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(offline)
        _run([str(executable), "sidecar", "serve", "--help"], cwd=project, env=env, expected=0)
        installed_api = _run(
            [
                sys.executable,
                "-c",
                (
                    "from mycelium import composite; "
                    "from mycelium.sidecar import SidecarConfig, openapi_document; "
                    "assert callable(composite); "
                    "assert SidecarConfig; "
                    "assert openapi_document()['paths']['/ready']['get']['security'] == []"
                ),
            ],
            cwd=project,
            env=env,
            expected=0,
        )
        if installed_api.stdout or installed_api.stderr:
            raise SystemExit("installed API smoke check produced unexpected output")
        command = [str(executable), "skills", "install", "--target", str(catalog)]

        installed = _run(command, cwd=project, env=env, expected=0)
        if "Installed mycelium-setup skill" not in installed.stdout:
            raise SystemExit(f"missing successful-install message: {installed.stdout!r}")
        if not sentinel.is_file():
            raise SystemExit("offline network guard was not loaded by the console script")
        if _files(destination) != canonical:
            raise SystemExit("installed skill differs byte-for-byte from canonical source")

        current = _run(command, cwd=project, env=env, expected=0)
        if "already current" not in current.stdout:
            raise SystemExit(f"missing idempotent-install message: {current.stdout!r}")

        conflict = destination / "local-conflict.txt"
        conflict.write_bytes(b"keep this local customization\n")
        before = _files(destination)
        refused = _run(command, cwd=project, env=env, expected=1)
        if "use --force to replace it" not in refused.stderr:
            raise SystemExit(f"missing conflict guidance: {refused.stderr!r}")
        if _files(destination) != before:
            raise SystemExit("non-force install modified the conflicting destination")

        forced = _run([*command, "--force"], cwd=project, env=env, expected=0)
        if "Installed mycelium-setup skill" not in forced.stdout:
            raise SystemExit(f"missing force-install message: {forced.stdout!r}")
        if _files(destination) != canonical:
            raise SystemExit("force install did not restore the exact canonical skill")

    print(
        "installed distribution passed sidecar, composite, offline, byte, conflict, and force checks"
    )


if __name__ == "__main__":
    main()
