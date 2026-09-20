#!/usr/bin/env python3
"""Check that the wheel and sdist build reproducibly (#190).

Builds ``sdk/`` twice from the same commit, each time in its own clean source
directory and its own throwaway virtual environment, then compares the two
wheels and the two sdists.

The build toolchain is pinned by ``.github/reproducible-build-requirements.txt``
so the only difference between the builds is the build itself. Nothing is
published and no credentials are used; installing the pinned toolchain only
needs read access to a package index.

What "the same" means
---------------------
The two builds pass when every archive member has the same path, type,
content, and executable bit, and the wheel/sdist metadata files agree. When the
archives are not byte-identical but match on all of that, the check still
passes and reports which normalized fields differed.

These fields are normalized because they do not change what gets installed:

* member modification times (zip and tar)
* tar owner fields: uid, gid, uname, gname
* member order inside the archive
* the gzip header (mtime, OS byte, embedded file name) of the sdist
* zip compression level, extra fields, creator OS, and permission bits other
  than the executable bit

Everything else is compared, including file contents, symlink targets, the
executable bit, and every metadata header (``METADATA``, ``WHEEL``,
``PKG-INFO``). Use ``--require-identical-bytes`` to also fail when the
archives differ byte-for-byte.

What is deliberately varied between the builds
----------------------------------------------
* the absolute path of the source tree (catches embedded build paths)
* the temporary and virtual-environment locations
* ``TZ`` and ``LC_ALL``
* ``PYTHONHASHSEED`` is left random, so set/dict ordering bugs show up

``SOURCE_DATE_EPOCH`` is set to the commit time for both builds, the standard
way to give a builder a fixed clock.

Usage::

    python .github/scripts/check-reproducible-build.py
    python .github/scripts/check-reproducible-build.py --commit HEAD~1 \\
        --report report.json --keep-artifacts repro-artifacts

Exit status: 0 reproducible, 1 unexpected differences, 2 the check could not
run (build, install, or export failed).
"""

from __future__ import annotations

import argparse
import dataclasses
import difflib
import email
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUIREMENTS = Path(__file__).resolve().parents[1] / "reproducible-build-requirements.txt"
PROJECT_SUBDIR = "sdk"

# Environment differences applied to build A and build B respectively.
BUILD_ENVIRONMENTS = (
    {"TZ": "UTC", "LC_ALL": "C"},
    {"TZ": "Pacific/Auckland", "LC_ALL": "C"},
)

MAX_DIFF_LINES = 30
# name==exact.version, optionally followed by an environment marker. Wildcards
# such as ``==1.*`` are not exact pins.
_PIN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[A-Za-z0-9][A-Za-z0-9._+-]*(\s*;.*)?$")
_METADATA_MEMBER = re.compile(
    r"(^|/)(PKG-INFO|METADATA|WHEEL)$"
)


class CheckError(Exception):
    """The check could not run to completion (exit status 2)."""


# --------------------------------------------------------------------------- #
# Archive model
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Member:
    """What is compared for one archive member, plus the fields we ignore."""

    kind: str  # "file" | "dir" | "symlink" | "other"
    size: int
    sha256: str
    executable: bool
    link: str
    content: bytes = dataclasses.field(repr=False, compare=False, default=b"")
    volatile: tuple = dataclasses.field(compare=False, default=())


@dataclasses.dataclass
class Archive:
    label: str  # "wheel" or "sdist"
    path: Path
    sha256: str
    members: dict[str, Member]
    order: list[str]
    container: tuple  # volatile archive-level fields (gzip header)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_name(name: str) -> str:
    while name.startswith("./"):
        name = name[2:]
    return name.rstrip("/") or "."


def read_wheel(path: Path) -> Archive:
    members: dict[str, Member] = {}
    order: list[str] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                name = _clean_name(info.filename)
                if name in members:
                    raise CheckError(f"{path.name}: duplicate member {name!r}")
                mode = (info.external_attr >> 16) & 0xFFFF
                if info.is_dir():
                    kind, data = "dir", b""
                elif mode and stat.S_ISLNK(mode):
                    kind, data = "symlink", b""
                    link = archive.read(info).decode("utf-8", "replace")
                    members[name] = Member(
                        kind, 0, "", False, link,
                        volatile=(info.date_time, info.create_system),
                    )
                    order.append(name)
                    continue
                else:
                    kind, data = "file", archive.read(info)
                members[name] = Member(
                    kind,
                    len(data),
                    _sha256(data) if kind == "file" else "",
                    bool(mode & 0o111) and kind == "file",
                    "",
                    content=data,
                    volatile=(info.date_time, info.create_system),
                )
                order.append(name)
    except zipfile.BadZipFile as exc:
        raise CheckError(f"{path.name}: not a valid wheel: {exc}") from exc
    return Archive("wheel", path, _file_sha256(path), members, order, ())


def _gzip_header(path: Path) -> tuple:
    head = path.read_bytes()[:10]
    if len(head) < 10 or head[:2] != b"\x1f\x8b":
        return ()
    return (int.from_bytes(head[4:8], "little"), head[9])  # (mtime, OS byte)


def read_sdist(path: Path) -> Archive:
    members: dict[str, Member] = {}
    order: list[str] = []
    try:
        with tarfile.open(path, mode="r:gz") as archive:
            for info in archive.getmembers():
                name = _clean_name(info.name)
                if name in members:
                    raise CheckError(f"{path.name}: duplicate member {name!r}")
                volatile = (info.mtime, info.uid, info.gid, info.uname, info.gname)
                if info.isdir():
                    member = Member("dir", 0, "", False, "", volatile=volatile)
                elif info.issym() or info.islnk():
                    member = Member(
                        "symlink", 0, "", False, info.linkname, volatile=volatile
                    )
                elif info.isfile():
                    handle = archive.extractfile(info)
                    data = handle.read() if handle is not None else b""
                    member = Member(
                        "file",
                        len(data),
                        _sha256(data),
                        bool(info.mode & 0o111),
                        "",
                        content=data,
                        volatile=volatile,
                    )
                else:
                    member = Member("other", 0, "", False, "", volatile=volatile)
                members[name] = member
                order.append(name)
    except tarfile.TarError as exc:
        raise CheckError(f"{path.name}: not a valid sdist: {exc}") from exc
    return Archive("sdist", path, _file_sha256(path), members, order, _gzip_header(path))


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class Difference:
    """An unexpected difference. Any of these fails the check."""

    kind: str  # missing | extra | type | content | mode | link | metadata
    member: str
    detail: str
    diff: list[str] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Comparison:
    label: str
    name_a: str
    name_b: str
    sha256_a: str
    sha256_b: str
    differences: list[Difference]
    normalized: list[str]  # expected differences that were normalized away

    @property
    def bytes_identical(self) -> bool:
        return self.sha256_a == self.sha256_b

    @property
    def reproducible(self) -> bool:
        return not self.differences


def _decode(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _limited(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_DIFF_LINES:
        return lines
    extra = len(lines) - MAX_DIFF_LINES
    return [*lines[:MAX_DIFF_LINES], f"... {extra} more diff lines"]


def _text_diff(member: str, a: bytes, b: bytes) -> list[str]:
    text_a, text_b = _decode(a), _decode(b)
    if text_a is None or text_b is None:
        return []
    diff = difflib.unified_diff(
        text_a.splitlines(),
        text_b.splitlines(),
        fromfile=f"build-A/{member}",
        tofile=f"build-B/{member}",
        lineterm="",
        n=1,
    )
    return _limited(list(diff))


def _headers(data: bytes) -> tuple[list[tuple[str, str]], str]:
    message = email.message_from_bytes(data)
    body = message.get_payload()
    return list(message.items()), body if isinstance(body, str) else ""


def _metadata_diff(a: bytes, b: bytes) -> list[str]:
    """Header-level comparison of METADATA / WHEEL / PKG-INFO files."""
    items_a, body_a = _headers(a)
    items_b, body_b = _headers(b)
    names = sorted({name for name, _ in items_a} | {name for name, _ in items_b})
    lines: list[str] = []
    for name in names:
        values_a = [value for key, value in items_a if key == name]
        values_b = [value for key, value in items_b if key == name]
        if values_a == values_b:
            continue
        if sorted(values_a) == sorted(values_b):
            lines.append(
                f"{name}: same {len(values_a)} value(s) in a different order "
                "(sort the source collection)"
            )
        else:
            for value in values_a:
                if value not in values_b:
                    lines.append(f"- {name}: {value}")
            for value in values_b:
                if value not in values_a:
                    lines.append(f"+ {name}: {value}")
    if body_a != body_b:
        lines.extend(_text_diff("description body", body_a.encode(), body_b.encode()))
    return _limited(lines)


def _member_difference(name: str, a: Member, b: Member) -> Difference | None:
    if a.kind != b.kind:
        return Difference("type", name, f"type changed: {a.kind} -> {b.kind}")
    if a.kind == "symlink" and a.link != b.link:
        return Difference("link", name, f"symlink target changed: {a.link!r} -> {b.link!r}")
    if a.kind == "file":
        if a.sha256 != b.sha256:
            detail = f"content differs ({a.size} -> {b.size} bytes)"
            if _METADATA_MEMBER.search(name):
                diff = _metadata_diff(a.content, b.content) or _text_diff(
                    name, a.content, b.content
                )
                return Difference("metadata", name, detail, diff)
            diff = _text_diff(name, a.content, b.content)
            if not diff:
                detail += f"; sha256 {a.sha256[:12]} -> {b.sha256[:12]} (binary)"
            return Difference("content", name, detail, diff)
        if a.executable != b.executable:
            return Difference(
                "mode",
                name,
                f"executable bit changed: {a.executable} -> {b.executable}",
            )
    return None


def compare_archives(a: Archive, b: Archive) -> Comparison:
    """Compare two builds of the same artifact, normalizing expected drift."""
    differences: list[Difference] = []
    for name in sorted(a.members.keys() - b.members.keys()):
        differences.append(
            Difference("missing", name, "present in build A, absent from build B")
        )
    for name in sorted(b.members.keys() - a.members.keys()):
        differences.append(
            Difference("extra", name, "absent from build A, present in build B")
        )
    for name in sorted(a.members.keys() & b.members.keys()):
        found = _member_difference(name, a.members[name], b.members[name])
        if found is not None:
            differences.append(found)

    others = [d for d in differences if not d.member.endswith(".dist-info/RECORD")]
    if others:
        for difference in differences:
            if difference.member.endswith(".dist-info/RECORD"):
                difference.detail += " (lists the hashes of the members above; a consequence)"

    normalized: list[str] = []
    shared = a.members.keys() & b.members.keys()
    if a.order != b.order and a.members.keys() == b.members.keys():
        normalized.append("member order differs")
    if a.label == "wheel":
        if any(a.members[n].volatile[0] != b.members[n].volatile[0] for n in shared):
            normalized.append("zip member timestamps differ")
    else:
        if any(a.members[n].volatile[0] != b.members[n].volatile[0] for n in shared):
            normalized.append("tar member mtimes differ")
        if any(a.members[n].volatile[1:] != b.members[n].volatile[1:] for n in shared):
            normalized.append("tar ownership (uid/gid/uname/gname) differs")
        if a.container != b.container:
            normalized.append("gzip header (mtime/OS) differs")
    if not differences and a.sha256 != b.sha256 and not normalized:
        normalized.append(
            "archives differ byte-for-byte in normalized-away framing "
            "(compression or zip/tar layout) but every member matches"
        )
    return Comparison(
        a.label, a.path.name, b.path.name, a.sha256, b.sha256, differences, normalized
    )


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

_HINTS = {
    "content": (
        "Generated or environment-dependent data reached the artifact. Look for "
        "timestamps, random ids, absolute paths, or values read from the build "
        "machine, and derive them from the commit (or SOURCE_DATE_EPOCH) instead."
    ),
    "metadata": (
        "Package metadata differs between builds. Look for unordered collections "
        "(sets, dict iteration, glob order) or values read from the environment "
        "when metadata is generated."
    ),
    "missing": (
        "A file exists in only one build. Look for build steps that depend on the "
        "working directory, on files created earlier in the build, or on glob order."
    ),
    "extra": (
        "A file exists in only one build. Look for build steps that depend on the "
        "working directory, on files created earlier in the build, or on glob order."
    ),
    "type": "A member changed between file, directory and symlink across builds.",
    "link": "A symlink target differs between builds; targets must not embed paths.",
    "mode": (
        "The executable bit differs. Set it explicitly instead of inheriting the "
        "build machine's file modes or umask."
    ),
}


def build_paths_in_diffs(comparisons: list[Comparison], roots: list[Path]) -> bool:
    """True when a differing line contains one of the build directories.

    Diffs of Python source often show paths through ``repr``, which doubles
    backslashes on Windows, so all three spellings are searched.
    """
    text = "\n".join(
        line for c in comparisons for d in c.differences for line in [d.detail, *d.diff]
    )
    for root in roots:
        plain = str(root)
        if plain in text or root.as_posix() in text or plain.replace("\\", "\\\\") in text:
            return True
    return False


def format_report(
    comparisons: list[Comparison],
    *,
    commit: str,
    source_date_epoch: str,
    requirements: Path,
    strict_bytes: bool,
    build_roots: list[Path],
) -> str:
    failed = _failed(comparisons, strict_bytes)
    lines = [
        f"Reproducible build check: {'FAILED' if failed else 'PASSED'}",
        f"  commit:            {commit}",
        f"  SOURCE_DATE_EPOCH: {source_date_epoch}",
        f"  pinned toolchain:  {requirements.name}",
        "",
    ]
    for comparison in comparisons:
        if comparison.differences:
            state = "NOT REPRODUCIBLE"
        elif comparison.bytes_identical:
            state = "reproducible (byte-identical)"
        elif strict_bytes:
            state = "NOT REPRODUCIBLE (bytes differ; --require-identical-bytes)"
        else:
            state = "reproducible (equivalent after normalization)"
        lines.append(f"{comparison.label}: {comparison.name_a}  ->  {state}")
        lines.append(f"  build A sha256: {comparison.sha256_a}")
        lines.append(f"  build B sha256: {comparison.sha256_b}")
        for note in comparison.normalized:
            lines.append(f"  normalized (expected): {note}")
        if comparison.differences:
            lines.append(f"  UNEXPECTED DIFFERENCES ({len(comparison.differences)}):")
            for difference in comparison.differences:
                lines.append(f"    [{difference.kind}] {difference.member}: {difference.detail}")
                lines.extend(f"        {row}" for row in difference.diff)
        lines.append("")

    if failed:
        kinds = {d.kind for c in comparisons for d in c.differences}
        lines.append("What to do:")
        for kind in sorted(kinds):
            lines.append(f"  - {kind}: {_HINTS.get(kind, 'Inspect the member above.')}")
        if build_paths_in_diffs(comparisons, build_roots):
            lines.append(
                "  - An absolute build directory appears in the differing content: "
                "something embeds the source path. Make it relative."
            )
        if strict_bytes and not any(c.differences for c in comparisons):
            lines.append(
                "  - Only byte-level framing differs. Drop --require-identical-bytes "
                "if that is acceptable, or pin the archive writer's timestamps."
            )
        lines.append(
            "  - Re-run with --keep-artifacts DIR and compare DIR/build-a with "
            "DIR/build-b (for example with diffoscope)."
        )
    return "\n".join(lines).rstrip() + "\n"


def _failed(comparisons: list[Comparison], strict_bytes: bool) -> bool:
    return any(
        c.differences or (strict_bytes and not c.bytes_identical) for c in comparisons
    )


def to_json(
    comparisons: list[Comparison], *, commit: str, strict_bytes: bool
) -> dict:
    return {
        "commit": commit,
        "reproducible": not _failed(comparisons, strict_bytes),
        "require_identical_bytes": strict_bytes,
        "artifacts": [
            {
                "kind": c.label,
                "build_a": {"file": c.name_a, "sha256": c.sha256_a},
                "build_b": {"file": c.name_b, "sha256": c.sha256_b},
                "bytes_identical": c.bytes_identical,
                "reproducible": c.reproducible,
                "normalized": c.normalized,
                "differences": [dataclasses.asdict(d) for d in c.differences],
            }
            for c in comparisons
        ],
    }


# --------------------------------------------------------------------------- #
# Pinned toolchain
# --------------------------------------------------------------------------- #


def _requirement_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[;\s]", requirement.strip(), maxsplit=1)[0].lower().replace("_", "-")


def read_pins(requirements: Path) -> list[str]:
    """Return the pinned requirement lines, refusing anything not exactly pinned."""
    if not requirements.is_file():
        raise CheckError(f"pinned toolchain file not found: {requirements}")
    pins: list[str] = []
    for number, raw in enumerate(requirements.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not _PIN.match(line):
            raise CheckError(
                f"{requirements.name}:{number}: '{line}' is not an exact pin "
                "(use name==version so the toolchain cannot drift)"
            )
        pins.append(line)
    return pins


def build_system_requires(pyproject: Path) -> list[str]:
    """Names listed in [build-system].requires (regex; avoids tomllib on 3.10)."""
    text = pyproject.read_text(encoding="utf-8")
    section = re.search(r"^\[build-system\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if section is None:
        raise CheckError(f"{pyproject}: no [build-system] table")
    requires = re.search(r"^requires\s*=\s*\[(.*?)\]", section.group(1), re.M | re.S)
    if requires is None:
        raise CheckError(f"{pyproject}: [build-system] has no requires list")
    return re.findall(r"""["']([^"']+)["']""", requires.group(1))


def check_pins_cover_build_system(pins: list[str], requires: list[str]) -> None:
    pinned = {_requirement_name(pin) for pin in pins}
    missing = sorted(
        {_requirement_name(req) for req in requires} - pinned
    )
    if missing:
        raise CheckError(
            "build-system requirements without a pin in the toolchain file: "
            + ", ".join(missing)
        )


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #


def _run(command: list[str], *, cwd: Path | None = None, env: dict | None = None) -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        command, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).strip().splitlines()[-25:])
        raise CheckError(
            f"command failed ({result.returncode}): {' '.join(command)}\n{tail}"
        )


def _venv_python(venv: Path) -> Path:
    scripts = "Scripts" if os.name == "nt" else "bin"
    return venv / scripts / ("python.exe" if os.name == "nt" else "python")


def create_toolchain(venv: Path, requirements: Path) -> Path:
    """Create a fresh virtual environment with only the pinned toolchain."""
    _run([sys.executable, "-m", "venv", str(venv)])
    python = _venv_python(venv)
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1"}
    _run([str(python), "-m", "pip", "install", "-r", str(requirements)], env=env)
    return python


def commit_facts(repo_root: Path, commit: str) -> tuple[str, str]:
    """Resolve the commit to a full SHA and its commit time (SOURCE_DATE_EPOCH)."""
    def git(*args: str) -> str:
        result = subprocess.run(  # noqa: S603, S607
            ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise CheckError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    sha = git("rev-parse", "--verify", f"{commit}^{{commit}}")
    return sha, git("log", "-1", "--format=%ct", sha)


def export_commit(repo_root: Path, sha: str, destination: Path) -> None:
    """Extract a clean copy of the commit (tracked files only) into destination."""
    destination.mkdir(parents=True)
    archive = destination.parent / f"{destination.name}.tar"
    _run(["git", "archive", "--format=tar", "--output", str(archive), sha], cwd=repo_root)
    with tarfile.open(archive) as bundle:
        if hasattr(tarfile, "data_filter"):
            bundle.extractall(destination, filter="data")  # noqa: S202
        else:  # pragma: no cover - interpreters older than the tar filter backport
            bundle.extractall(destination)  # noqa: S202
    archive.unlink()


def build_once(
    label: str,
    *,
    repo_root: Path,
    sha: str,
    source_date_epoch: str,
    requirements: Path,
    workdir: Path,
    variation: dict[str, str],
) -> Path:
    """Export the commit, create a toolchain, and build; return the output dir."""
    # A different absolute path per build is intentional: it exposes builds that
    # embed the source directory.
    source = workdir / f"source-{label}-{sha[:7]}"
    export_commit(repo_root, sha, source)
    python = create_toolchain(workdir / f"venv-{label}", requirements)
    out = workdir / f"out-{label}"
    out.mkdir()
    env = {**os.environ, **variation, "SOURCE_DATE_EPOCH": source_date_epoch}
    env.pop("PYTHONHASHSEED", None)  # keep hash randomization on purpose
    _run(
        [str(python), "-m", "build", "--no-isolation", "--outdir", str(out)],
        cwd=source / PROJECT_SUBDIR,
        env=env,
    )
    return out


def pick_artifacts(out: Path) -> tuple[Path, Path]:
    wheels = sorted(out.glob("*.whl"))
    sdists = sorted(out.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise CheckError(
            f"expected exactly one wheel and one sdist in {out}, found "
            f"{[p.name for p in wheels]} and {[p.name for p in sdists]}"
        )
    return wheels[0], sdists[0]


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def annotate(message: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::error title=Reproducible build check::{message}", file=sys.stderr)


def run_check(args: argparse.Namespace) -> int:
    repo_root = args.repo_root.resolve()
    pins = read_pins(args.requirements)
    check_pins_cover_build_system(
        pins, build_system_requires(repo_root / PROJECT_SUBDIR / "pyproject.toml")
    )
    sha, source_date_epoch = commit_facts(repo_root, args.commit)
    print(f"Building {PROJECT_SUBDIR}/ twice from {sha[:12]} (SOURCE_DATE_EPOCH={source_date_epoch})")

    with tempfile.TemporaryDirectory(
        prefix="mycelium-repro-", ignore_cleanup_errors=True
    ) as scratch:
        workdir = Path(args.workdir).resolve() if args.workdir else Path(scratch)
        if args.workdir:
            workdir.mkdir(parents=True, exist_ok=True)
        outputs = []
        for label, variation in zip(("a", "b"), BUILD_ENVIRONMENTS, strict=True):
            print(f"  building {label} ...", flush=True)
            outputs.append(
                build_once(
                    label,
                    repo_root=repo_root,
                    sha=sha,
                    source_date_epoch=source_date_epoch,
                    requirements=args.requirements,
                    workdir=workdir,
                    variation=variation,
                )
            )
        wheel_a, sdist_a = pick_artifacts(outputs[0])
        wheel_b, sdist_b = pick_artifacts(outputs[1])
        comparisons = [
            compare_archives(read_wheel(wheel_a), read_wheel(wheel_b)),
            compare_archives(read_sdist(sdist_a), read_sdist(sdist_b)),
        ]
        if args.keep_artifacts:
            for label, out in zip(("build-a", "build-b"), outputs, strict=True):
                target = args.keep_artifacts / label
                shutil.copytree(out, target, dirs_exist_ok=True)
        # ``python -m build`` unpacks the sdist under the system temp directory
        # to build the wheel, so a leaked path can point there as well.
        build_roots = [workdir, Path(tempfile.gettempdir())]
        report = format_report(
            comparisons,
            commit=sha,
            source_date_epoch=source_date_epoch,
            requirements=args.requirements,
            strict_bytes=args.require_identical_bytes,
            build_roots=build_roots,
        )

    print(report, end="")
    if args.report:
        args.report.write_text(
            json.dumps(
                to_json(comparisons, commit=sha, strict_bytes=args.require_identical_bytes),
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    if _failed(comparisons, args.require_identical_bytes):
        for comparison in comparisons:
            for difference in comparison.differences:
                annotate(f"{comparison.label}: [{difference.kind}] {difference.member}: {difference.detail}")
            if args.require_identical_bytes and not comparison.bytes_identical:
                annotate(f"{comparison.label}: archives are not byte-identical")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the wheel and sdist twice and verify they match.",
    )
    parser.add_argument("--commit", default="HEAD", help="commit to build (default: HEAD)")
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    parser.add_argument(
        "--requirements",
        type=Path,
        default=DEFAULT_REQUIREMENTS,
        help="pinned build toolchain (default: .github/reproducible-build-requirements.txt)",
    )
    parser.add_argument("--report", type=Path, help="write a JSON report to this path")
    parser.add_argument(
        "--keep-artifacts",
        type=Path,
        metavar="DIR",
        help="copy both builds' outputs to DIR/build-a and DIR/build-b for inspection",
    )
    parser.add_argument(
        "--require-identical-bytes",
        action="store_true",
        help="also fail when the archives are not byte-for-byte identical",
    )
    parser.add_argument("--workdir", help="build here instead of a temporary directory")
    args = parser.parse_args(argv)
    try:
        return run_check(args)
    except CheckError as exc:
        print(f"error: {exc}", file=sys.stderr)
        annotate(str(exc).splitlines()[0])
        return 2


if __name__ == "__main__":
    sys.exit(main())
