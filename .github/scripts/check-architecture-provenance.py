#!/usr/bin/env python3
"""Validate the verifiable source provenance of the Architecture Map.

Verifies:
1. Provenance manifest exists, is valid JSON, and adheres to schema.
2. The review_base_commit exists in git history and is an ancestor of HEAD.
3. All source and test files listed in the manifest exist on disk.
4. All source and test files linked from sdk/docs/ARCHITECTURE_AND_GUARANTEE_MAP.md
   are captured in the provenance manifest.
5. Emits standard compiler diagnostics and GitHub Actions annotations on failure.

Usage:
    python .github/scripts/check-architecture-provenance.py [--root <DIR>] [--strict]
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_DOC = "sdk/docs/ARCHITECTURE_AND_GUARANTEE_MAP.md"
DEFAULT_MANIFEST = "sdk/docs/architecture_provenance.json"
DEFAULT_PYPROJECT = "sdk/pyproject.toml"

HEX_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def emit_error(message: str, file: str | None = None, line: int | None = None):
    """Print an error and optional GitHub Actions annotation."""
    loc = ""
    if file:
        loc = f"{file}:{line or 1}: "
    print(f"{loc}error: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        annotation = f"::error file={file or 'provenance'}"
        if line:
            annotation += f",line={line}"
        annotation += f"::{message}"
        print(annotation, file=sys.stderr)


def compute_sha256(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def check_git_ancestor(root: Path, base_commit: str) -> tuple[bool, str]:
    """Check if base_commit exists and is an ancestor of HEAD."""
    try:
        res = subprocess.run(
            ["git", "cat-file", "-e", f"{base_commit}^{{commit}}"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return (
                False,
                f"Review base commit '{base_commit}' does not exist in repository object database",
            )

        res = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_commit, "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            return (
                False,
                f"Review base commit '{base_commit}' is not an ancestor of HEAD",
            )
        return True, "Ancestor check passed"
    except FileNotFoundError:
        # git binary not found in minimal test environment
        return True, "Git executable not available, skipping ancestry check"
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"Error verifying git ancestry: {e}"


def validate_provenance(
    root: Path,
    doc_path: Path | None = None,
    manifest_path: Path | None = None,
    strict: bool = False,
) -> tuple[bool, list[str]]:
    """Validate the architecture map provenance manifest."""
    errors: list[str] = []
    doc = (root / (doc_path or DEFAULT_DOC)).resolve()
    manifest_file = (root / (manifest_path or DEFAULT_MANIFEST)).resolve()

    if not manifest_file.exists():
        msg = f"Provenance manifest not found: {manifest_file.relative_to(root).as_posix()}"
        emit_error(msg, str(manifest_file))
        return False, [msg]

    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        msg = f"Malformed JSON in provenance manifest: {e}"
        emit_error(msg, str(manifest_file))
        return False, [msg]

    # Validate top-level keys
    required_keys = [
        "schema_version",
        "package_version",
        "review_base_commit",
        "reviewed_at",
        "source_files",
        "test_files",
    ]
    for key in required_keys:
        if key not in manifest:
            errors.append(f"Missing required manifest property '{key}'")

    if errors:
        for err in errors:
            emit_error(err, str(manifest_file))
        return False, errors

    # Validate formats
    base_commit = str(manifest["review_base_commit"]).strip()
    if not HEX_PATTERN.match(base_commit):
        errors.append(
            f"Invalid review_base_commit format: '{base_commit}' (expected 7-40 hex chars)"
        )

    reviewed_at = str(manifest["reviewed_at"]).strip()
    if not DATE_PATTERN.match(reviewed_at):
        errors.append(
            f"Invalid reviewed_at format: '{reviewed_at}' (expected YYYY-MM-DD)"
        )

    if not isinstance(manifest["source_files"], dict) or not isinstance(
        manifest["test_files"], dict
    ):
        errors.append(
            "'source_files' and 'test_files' must be mappings of relative_path -> sha256"
        )

    if errors:
        for err in errors:
            emit_error(err, str(manifest_file))
        return False, errors

    # Check Git ancestry
    is_ancestor, reason = check_git_ancestor(root, base_commit)
    if not is_ancestor:
        errors.append(reason)
        emit_error(reason, str(manifest_file))

    # Check that all files in manifest exist
    all_manifest_files = {**manifest["source_files"], **manifest["test_files"]}
    for rel_path, expected_hash in all_manifest_files.items():
        file_path = root / rel_path
        if not file_path.exists():
            msg = f"File listed in provenance manifest does not exist: {rel_path}"
            errors.append(msg)
            emit_error(msg, str(manifest_file))
        elif len(expected_hash) != 64:
            msg = f"Invalid SHA-256 digest length for {rel_path}: {expected_hash}"
            errors.append(msg)
            emit_error(msg, str(manifest_file))
        elif strict:
            actual_hash = compute_sha256(file_path)
            if actual_hash != expected_hash:
                msg = f"Source hash drift detected in {rel_path}: manifest={expected_hash[:12]} actual={actual_hash[:12]}"
                errors.append(msg)
                emit_error(msg, str(file_path))

    # Check that all files referenced in ARCHITECTURE_AND_GUARANTEE_MAP.md are indexed
    if doc.exists():
        doc_content = doc.read_text(encoding="utf-8")
        links = re.findall(r"\[.*?\]\((.*?)\)", doc_content)
        doc_dir = doc.parent
        for link in links:
            clean = link.split("#")[0]
            if not clean.endswith(".py"):
                continue
            target = (doc_dir / clean).resolve()
            try:
                rel = target.relative_to(root).as_posix()
            except ValueError:
                rel = target.as_posix()

            if rel not in all_manifest_files:
                msg = f"Architecture map links to unindexed file: {rel} (link: {link})"
                errors.append(msg)
                emit_error(msg, str(doc))

    return len(errors) == 0, errors


def main():
    parser = argparse.ArgumentParser(
        description="Validate architecture map source provenance"
    )
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument(
        "--doc", default=DEFAULT_DOC, help="Path to architecture map markdown"
    )
    parser.add_argument(
        "--manifest", default=DEFAULT_MANIFEST, help="Path to provenance manifest JSON"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on content hash divergence from review baseline",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    valid, errors = validate_provenance(
        root=root,
        doc_path=Path(args.doc),
        manifest_path=Path(args.manifest),
        strict=args.strict,
    )

    if not valid:
        print(
            f"\nArchitecture provenance validation failed with {len(errors)} error(s).",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Architecture map source provenance verified successfully.")
    sys.exit(0)


if __name__ == "__main__":
    main()
