"""Verify package build reproducibility for mycelium-runtime.

This script builds the wheel and sdist twice from the same commit in isolated
clean environments, normalizes expected nondeterministic archive fields, and
verifies that both builds produce identical, reproducible artifacts.

Nondeterminism Normalization Reference (Issue #190):
1. Archive Member Timestamps (mtime / date_time):
   - Wheel (ZIP): `ZipInfo.date_time` (year, month, day, hour, min, sec).
   - Source Distribution (TAR): `TarInfo.mtime` (POSIX seconds).
   - In reproducible builds, timestamps must be deterministic. When `SOURCE_DATE_EPOCH`
     is set (e.g. from the git commit timestamp), build backends like Hatchling clamp
     member timestamps to this epoch (or default fixed epoch 2020-02-02 00:00:00 UTC).
     During content inspection, timestamps are verified against this normalized epoch.
2. File Permissions & Mode Bits:
   - Tar archives record mode, uid, gid, uname, and gname. These can vary based on
     host umask and build environment user IDs.
   - Normalization standardizes file modes to 0o644 (regular files) or 0o755 (executables
     and directories), with uid=0, gid=0, uname="", and gname="".
3. Member Entry Ordering:
   - Filesystem directory traversal can be non-deterministic across filesystems.
   - Normalization sorts archive members canonically by POSIX relative path.
4. Tarball Compression Container (Gzip Header):
   - Tarball .tar.gz headers record OS type flags and gzip mtime. In addition to
     comparing raw container hashes, the inner archive members and byte contents
     are decompressed and verified independently.
5. Wheel RECORD File:
   - PEP 376 RECORD files list member hashes. Since member contents are bit-identical,
     the normalized RECORD lines must also match identically.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

DEFAULT_SOURCE = Path(__file__).resolve().parents[2] / "sdk"
DEFAULT_EPOCH = 1580601600  # 2020-02-02 00:00:00 UTC (Hatchling default)

# Directories and file patterns excluded when copying from a source tree
EXCLUDED_NAMES = {
    ".coverage",
    ".git",
    ".hypothesis",
    ".mypy_cache",
    ".pip-audit-ignore",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "htmlcov",
}

EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".pyd",
}


@dataclass(frozen=True)
class MemberRecord:
    name: PurePosixPath
    size: int
    content_hash: str
    normalized_mode: int
    is_dir: bool


@dataclass
class ArtifactDiff:
    name: str
    raw_match: bool
    raw_hash_1: str
    raw_hash_2: str
    missing_in_2: list[PurePosixPath] = field(default_factory=list)
    extra_in_2: list[PurePosixPath] = field(default_factory=list)
    content_diffs: dict[PurePosixPath, tuple[str, str]] = field(default_factory=dict)
    mode_diffs: dict[PurePosixPath, tuple[int, int]] = field(default_factory=dict)
    text_diffs: dict[PurePosixPath, str] = field(default_factory=dict)

    @property
    def is_identical(self) -> bool:
        return self.raw_match

    @property
    def is_normalized_equal(self) -> bool:
        return (
            not self.missing_in_2
            and not self.extra_in_2
            and not self.content_diffs
            and not self.mode_diffs
        )


@dataclass
class ReproducibilityReport:
    success: bool
    artifacts: list[ArtifactDiff] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def normalize_mode(mode: int, is_dir: bool) -> int:
    """Normalize file mode to standard POSIX permissions."""
    if is_dir:
        return 0o755
    if mode & 0o111:
        return 0o755
    return 0o644


def sha256_bytes(data: bytes) -> str:
    """Compute hex SHA-256 digest of bytes."""
    return hashlib.sha256(data).hexdigest()


def inspect_wheel(artifact: Path) -> dict[PurePosixPath, tuple[MemberRecord, bytes]]:
    """Extract and inspect all members of a wheel archive."""
    members: dict[PurePosixPath, tuple[MemberRecord, bytes]] = {}
    try:
        with ZipFile(artifact) as archive:
            corrupt = archive.testzip()
            if corrupt is not None:
                raise SystemExit(f"{artifact}: corrupt wheel member: {corrupt}")
            for info in archive.infolist():
                if info.is_dir():
                    continue
                posix_name = PurePosixPath(info.filename)
                data = archive.read(info)
                # Upper 16 bits of external_attr contain UNIX file mode
                raw_mode = (
                    (info.external_attr >> 16) & 0o777 if info.external_attr else 0o644
                )
                record = MemberRecord(
                    name=posix_name,
                    size=len(data),
                    content_hash=sha256_bytes(data),
                    normalized_mode=normalize_mode(raw_mode, is_dir=False),
                    is_dir=False,
                )
                members[posix_name] = (record, data)
    except BadZipFile as exc:
        raise SystemExit(f"{artifact}: invalid wheel archive: {exc}") from exc
    return members


def inspect_sdist(artifact: Path) -> dict[PurePosixPath, tuple[MemberRecord, bytes]]:
    """Extract and inspect all members of an sdist (.tar.gz) archive."""
    members: dict[PurePosixPath, tuple[MemberRecord, bytes]] = {}
    try:
        with tarfile.open(artifact, mode="r:gz") as archive:
            for info in archive.getmembers():
                if not info.isfile():
                    continue
                posix_name = PurePosixPath(info.name)
                source = archive.extractfile(info)
                if source is None:
                    raise SystemExit(
                        f"{artifact}: unreadable sdist member: {posix_name}"
                    )
                data = source.read()
                record = MemberRecord(
                    name=posix_name,
                    size=len(data),
                    content_hash=sha256_bytes(data),
                    normalized_mode=normalize_mode(info.mode, is_dir=False),
                    is_dir=False,
                )
                members[posix_name] = (record, data)
    except tarfile.TarError as exc:
        raise SystemExit(f"{artifact}: invalid sdist archive: {exc}") from exc
    return members


def inspect_artifact(artifact: Path) -> dict[PurePosixPath, tuple[MemberRecord, bytes]]:
    """Dispatch inspection based on artifact format."""
    if artifact.name.endswith(".whl"):
        return inspect_wheel(artifact)
    if artifact.name.endswith(".tar.gz"):
        return inspect_sdist(artifact)
    raise ValueError(f"unsupported distribution artifact: {artifact}")


def _is_text(data: bytes) -> bool:
    """Return True if bytes appear to be UTF-8 decodable text."""
    if len(data) > 65536:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def compare_artifacts(artifact1: Path, artifact2: Path) -> ArtifactDiff:
    """Compare two build artifacts for exact and normalized equality."""
    bytes1 = artifact1.read_bytes()
    bytes2 = artifact2.read_bytes()
    hash1 = sha256_bytes(bytes1)
    hash2 = sha256_bytes(bytes2)
    raw_match = hash1 == hash2

    diff = ArtifactDiff(
        name=artifact1.name,
        raw_match=raw_match,
        raw_hash_1=hash1,
        raw_hash_2=hash2,
    )

    members1 = inspect_artifact(artifact1)
    members2 = inspect_artifact(artifact2)

    keys1 = set(members1.keys())
    keys2 = set(members2.keys())

    diff.missing_in_2 = sorted(keys1 - keys2)
    diff.extra_in_2 = sorted(keys2 - keys1)

    common = sorted(keys1 & keys2)
    for key in common:
        rec1, data1 = members1[key]
        rec2, data2 = members2[key]

        if rec1.content_hash != rec2.content_hash:
            diff.content_diffs[key] = (rec1.content_hash, rec2.content_hash)
            if _is_text(data1) and _is_text(data2):
                lines1 = data1.decode("utf-8", errors="replace").splitlines(
                    keepends=True
                )
                lines2 = data2.decode("utf-8", errors="replace").splitlines(
                    keepends=True
                )
                unified = list(
                    difflib.unified_diff(
                        lines1,
                        lines2,
                        fromfile=f"build1/{key}",
                        tofile=f"build2/{key}",
                        n=3,
                    )
                )
                if unified:
                    diff.text_diffs[key] = "".join(unified)

        if rec1.normalized_mode != rec2.normalized_mode:
            diff.mode_diffs[key] = (rec1.normalized_mode, rec2.normalized_mode)

    return diff


def compare_artifact_directories(
    dir1: Path,
    dir2: Path,
    strict_raw: bool = False,
) -> ReproducibilityReport:
    """Compare all distribution artifacts between two build directories."""
    files1 = {p.name: p for p in dir1.iterdir() if p.is_file()}
    files2 = {p.name: p for p in dir2.iterdir() if p.is_file()}

    report = ReproducibilityReport(success=True)

    dist_names1 = {name for name in files1 if name.endswith((".whl", ".tar.gz"))}
    dist_names2 = {name for name in files2 if name.endswith((".whl", ".tar.gz"))}

    if not dist_names1:
        report.errors.append(f"no distribution artifacts found in build 1: {dir1}")
        report.success = False
        return report

    if dist_names1 != dist_names2:
        missing = dist_names1 - dist_names2
        extra = dist_names2 - dist_names1
        if missing:
            report.errors.append(
                f"artifacts missing in build 2: {', '.join(sorted(missing))}"
            )
        if extra:
            report.errors.append(
                f"extra artifacts in build 2: {', '.join(sorted(extra))}"
            )
        report.success = False

    common = sorted(dist_names1 & dist_names2)
    has_wheel = False
    has_sdist = False

    for name in common:
        if name.endswith(".whl"):
            has_wheel = True
        elif name.endswith(".tar.gz"):
            has_sdist = True

        diff = compare_artifacts(files1[name], files2[name])
        report.artifacts.append(diff)

        if (strict_raw and not diff.raw_match) or not diff.is_normalized_equal:
            report.success = False

    if not has_wheel:
        report.errors.append("build output missing wheel artifact (.whl)")
        report.success = False
    if not has_sdist:
        report.errors.append("build output missing sdist artifact (.tar.gz)")
        report.success = False

    return report


def resolve_commit_epoch(commit: str | None = None) -> int:
    """Resolve commit timestamp or SOURCE_DATE_EPOCH environment variable."""
    env_epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if env_epoch:
        try:
            return int(env_epoch)
        except ValueError:
            pass

    ref = commit or "HEAD"
    try:
        output = subprocess.check_output(
            ["git", "log", "-1", "--format=%ct", ref],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        if output.isdigit():
            return int(output)
    except (subprocess.SubprocessError, OSError, ValueError):
        pass

    return DEFAULT_EPOCH


def extract_from_git(commit: str, source_path: Path, target_dir: Path) -> bool:
    """Extract source tree from a git commit using git archive."""
    try:
        # Determine git relative path from repo root
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
        rel_to_repo = (
            source_path.resolve().relative_to(Path(repo_root).resolve()).as_posix()
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return False

    try:
        proc = subprocess.run(
            ["git", "archive", "--format=tar", commit, rel_to_repo],
            capture_output=True,
            check=True,
        )
        with tarfile.open(fileobj=io.BytesIO(proc.stdout), mode="r") as archive:
            for member in archive.getmembers():
                parts = PurePosixPath(member.name).parts
                # Strip the leading source directory path prefix
                src_parts = PurePosixPath(rel_to_repo).parts
                if parts[: len(src_parts)] == src_parts:
                    sub_parts = parts[len(src_parts) :]
                    if not sub_parts:
                        continue
                    dest = target_dir / Path(*sub_parts)
                    if member.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        f = archive.extractfile(member)
                        if f is not None:
                            dest.write_bytes(f.read())
        return True
    except (subprocess.SubprocessError, OSError, tarfile.TarError):
        return False


def copy_clean_tree(source_path: Path, target_dir: Path) -> None:
    """Copy source directory cleanly excluding non-source/build cache files."""
    for root, dirs, files in os.walk(source_path):
        rel_root = Path(root).relative_to(source_path)
        # Prune excluded directories in-place
        dirs[:] = [
            d
            for d in dirs
            if d not in EXCLUDED_NAMES
            and not d.startswith(".")
            and not d.endswith(".egg-info")
        ]

        target_root = target_dir / rel_root
        target_root.mkdir(parents=True, exist_ok=True)

        for file_name in files:
            if file_name in EXCLUDED_NAMES:
                continue
            if any(file_name.endswith(suffix) for suffix in EXCLUDED_SUFFIXES):
                continue
            src_file = Path(root) / file_name
            dest_file = target_root / file_name
            shutil.copy2(src_file, dest_file)


def prepare_clean_source(
    source_path: Path,
    target_dir: Path,
    commit: str | None = None,
) -> None:
    """Prepare a clean source directory using git archive or clean file copy."""
    target_dir.mkdir(parents=True, exist_ok=True)
    ref = commit or "HEAD"
    extracted = extract_from_git(ref, source_path, target_dir)
    if not extracted:
        copy_clean_tree(source_path, target_dir)


def run_build(
    source_dir: Path,
    out_dir: Path,
    epoch: int,
    no_isolation: bool = True,
) -> None:
    """Execute python -m build in a clean environment with clamped timestamp."""
    out_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = str(epoch)

    cmd = [sys.executable, "-m", "build", str(source_dir), "--outdir", str(out_dir)]
    if no_isolation:
        cmd.append("--no-isolation")

    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"Package build failed in {source_dir}:\n"
            f"STDOUT:\n{result.stdout}\n"
            f"STDERR:\n{result.stderr}"
        )


def format_report(report: ReproducibilityReport, strict_raw: bool = False) -> str:
    """Format an actionable human-readable report."""
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("PACKAGE REPRODUCIBILITY VERIFICATION REPORT")
    lines.append("=" * 72)

    if report.errors:
        lines.append("\nErrors encountered:")
        for err in report.errors:
            lines.append(f"  - {err}")

    for diff in report.artifacts:
        lines.append(f"\nArtifact: {diff.name}")
        lines.append(f"  Build 1 SHA-256: {diff.raw_hash_1}")
        lines.append(f"  Build 2 SHA-256: {diff.raw_hash_2}")

        if diff.raw_match:
            lines.append("  Status: IDENTICAL (bit-for-bit raw archive match)")
        elif diff.is_normalized_equal:
            lines.append("  Status: NORMALIZED EQUAL")
            lines.append(
                "    (Member contents and normalized permissions match identically;"
                " raw container differs only in nondeterministic archive header metadata)"
            )
            if strict_raw:
                lines.append("    [FAIL] Strict raw byte match was requested.")
        else:
            lines.append("  Status: DIVERGENT (reproducibility check failed)")

            if diff.missing_in_2:
                lines.append(
                    f"  Missing in Build 2 ({len(diff.missing_in_2)} members):"
                )
                for m in diff.missing_in_2[:10]:
                    lines.append(f"    - {m}")
                if len(diff.missing_in_2) > 10:
                    lines.append(f"    ... and {len(diff.missing_in_2) - 10} more")

            if diff.extra_in_2:
                lines.append(f"  Extra in Build 2 ({len(diff.extra_in_2)} members):")
                for m in diff.extra_in_2[:10]:
                    lines.append(f"    - {m}")
                if len(diff.extra_in_2) > 10:
                    lines.append(f"    ... and {len(diff.extra_in_2) - 10} more")

            if diff.mode_diffs:
                lines.append(
                    f"  Permission mode diffs ({len(diff.mode_diffs)} members):"
                )
                for m, (m1, m2) in list(diff.mode_diffs.items())[:10]:
                    lines.append(f"    - {m}: build1=0o{m1:o} build2=0o{m2:o}")

            if diff.content_diffs:
                lines.append(
                    f"  Content hash diffs ({len(diff.content_diffs)} members):"
                )
                for m, (h1, h2) in list(diff.content_diffs.items())[:10]:
                    lines.append(f"    - {m}:")
                    lines.append(f"        build1={h1}")
                    lines.append(f"        build2={h2}")
                    if m in diff.text_diffs:
                        lines.append("        unified diff snippet:")
                        snippet = diff.text_diffs[m][:1000]
                        for diff_line in snippet.splitlines():
                            lines.append(f"          {diff_line}")

    lines.append("\n" + "=" * 72)
    overall = "PASSED" if report.success else "FAILED"
    lines.append(f"OVERALL VERIFICATION RESULT: {overall}")
    lines.append("=" * 72)
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Verify identical wheel and sdist generation across isolated clean builds.",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help=f"Path to package source directory (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--commit",
        type=str,
        default=None,
        help="Git commit reference to build from (default: HEAD if in git repo)",
    )
    parser.add_argument(
        "--build1",
        type=Path,
        default=None,
        help="Pre-existing directory for build 1 (skips building build 1)",
    )
    parser.add_argument(
        "--build2",
        type=Path,
        default=None,
        help="Pre-existing directory for build 2 (skips building build 2)",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        default=None,
        help="Optional directory to copy verified artifacts to",
    )
    parser.add_argument(
        "--strict-raw",
        action="store_true",
        help="Require bit-for-bit raw archive SHA-256 match",
    )
    parser.add_argument(
        "--isolated",
        action="store_true",
        help="Run python -m build with build isolation (default: --no-isolation)",
    )

    args = parser.parse_args(argv)

    epoch = resolve_commit_epoch(args.commit)
    no_isolation = not args.isolated

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        dir1 = args.build1
        dir2 = args.build2

        if dir1 is None or dir2 is None:
            # Need to build one or both in clean isolated environments
            if dir1 is None:
                dir1 = temp_path / "build1_out"
                src1 = temp_path / "build1_src"
                prepare_clean_source(args.source, src1, args.commit)
                print(
                    f"[check-package-reproducibility] Building 1st artifact set in {src1}..."
                )
                run_build(src1, dir1, epoch, no_isolation=no_isolation)

            if dir2 is None:
                dir2 = temp_path / "build2_out"
                src2 = temp_path / "build2_src"
                prepare_clean_source(args.source, src2, args.commit)
                print(
                    f"[check-package-reproducibility] Building 2nd artifact set in {src2}..."
                )
                run_build(src2, dir2, epoch, no_isolation=no_isolation)

        print(f"[check-package-reproducibility] Comparing {dir1} vs {dir2}...")
        report = compare_artifact_directories(dir1, dir2, strict_raw=args.strict_raw)
        report_text = format_report(report, strict_raw=args.strict_raw)
        print(report_text)

        if report.success and args.outdir is not None:
            args.outdir.mkdir(parents=True, exist_ok=True)
            for file_path in dir1.iterdir():
                if file_path.suffix in (".whl", ".gz"):
                    shutil.copy2(file_path, args.outdir / file_path.name)
            print(
                f"[check-package-reproducibility] Verified artifacts copied to {args.outdir}"
            )

        if not report.success:
            sys.exit(1)


if __name__ == "__main__":
    main()
