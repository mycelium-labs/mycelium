"""Tests for architecture map verifiable source provenance and drift detection."""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = ROOT / ".github" / "scripts"


def _load_script_module(name: str, filename: str):
    """Dynamically import helper script from .github/scripts/ without sys.path pollution."""
    script_path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check_mod = _load_script_module("check_arch_prov", "check-architecture-provenance.py")
update_mod = _load_script_module("update_arch_prov", "update-architecture-provenance.py")


class TestArchitectureProvenance:
    """Validate architecture provenance manifest and verification logic."""

    def test_live_provenance_manifest_is_valid(self):
        """The checked-in provenance manifest must validate cleanly against repository HEAD."""
        valid, errors = check_mod.validate_provenance(
            root=ROOT,
            doc_path=Path("sdk/docs/ARCHITECTURE_AND_GUARANTEE_MAP.md"),
            manifest_path=Path("sdk/docs/architecture_provenance.json"),
            strict=True,
        )
        assert valid, f"Provenance validation failed: {errors}"
        assert len(errors) == 0

    def test_manifest_structure_and_types(self):
        """Manifest JSON must conform strictly to expected fields, types, and hash formats."""
        manifest_path = ROOT / "sdk/docs/architecture_provenance.json"
        assert manifest_path.exists(), "architecture_provenance.json must exist"

        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert data["schema_version"] == 1
        assert isinstance(data["package_version"], str) and len(data["package_version"]) > 0
        assert isinstance(data["review_base_commit"], str) and len(data["review_base_commit"]) >= 7
        assert check_mod.DATE_PATTERN.match(data["reviewed_at"])

        source_files = data["source_files"]
        test_files = data["test_files"]
        assert len(source_files) >= 15
        assert len(test_files) >= 25

        for path, h in {**source_files, **test_files}.items():
            assert (ROOT / path).exists(), f"Tracked file must exist: {path}"
            assert len(h) == 64, f"SHA-256 hash must be 64 hex chars: {h}"
            assert int(h, 16) >= 0  # valid hex

    def test_git_ancestry_verification_success(self):
        """Review base commit must be recognized as an ancestor of current HEAD."""
        manifest_path = ROOT / "sdk/docs/architecture_provenance.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        base_commit = data["review_base_commit"]

        is_ancestor, reason = check_mod.check_git_ancestor(ROOT, base_commit)
        assert is_ancestor, f"Expected {base_commit} to be an ancestor of HEAD: {reason}"

    def test_git_ancestry_verification_nonexistent_commit(self):
        """Non-existent commit must be rejected with diagnostic."""
        bogus_commit = "0000000000000000000000000000000000000000"
        is_ancestor, reason = check_mod.check_git_ancestor(ROOT, bogus_commit)
        assert not is_ancestor
        assert "does not exist" in reason

    def test_missing_manifest_rejected(self, tmp_path):
        """Missing manifest path fails with clean error."""
        nonexistent = tmp_path / "missing.json"
        valid, errors = check_mod.validate_provenance(root=ROOT, manifest_path=nonexistent)
        assert not valid
        assert any("Provenance manifest not found" in e for e in errors)

    def test_malformed_json_rejected(self, tmp_path):
        """Invalid JSON syntax must be caught and rejected."""
        bad_json = tmp_path / "bad.json"
        bad_json.write_text("{ unclosed json: ", encoding="utf-8")
        valid, errors = check_mod.validate_provenance(root=ROOT, manifest_path=bad_json)
        assert not valid
        assert any("Malformed JSON" in e for e in errors)

    def test_missing_required_keys_rejected(self, tmp_path):
        """Missing required top-level keys must trigger validation errors."""
        incomplete = tmp_path / "incomplete.json"
        incomplete.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
        valid, errors = check_mod.validate_provenance(root=ROOT, manifest_path=incomplete)
        assert not valid
        assert any("Missing required manifest property" in e for e in errors)

    def test_invalid_date_or_commit_format_rejected(self, tmp_path):
        """Non-hex commit or invalid date format must be rejected."""
        invalid_fmt = tmp_path / "invalid_fmt.json"
        invalid_fmt.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "package_version": "1.38.1",
                    "review_base_commit": "not_a_hex_sha!",
                    "reviewed_at": "yesterday",
                    "source_files": {},
                    "test_files": {},
                }
            ),
            encoding="utf-8",
        )
        valid, errors = check_mod.validate_provenance(root=ROOT, manifest_path=invalid_fmt)
        assert not valid
        assert any("Invalid review_base_commit format" in e for e in errors)
        assert any("Invalid reviewed_at format" in e for e in errors)

    def test_detects_unindexed_linked_files(self, tmp_path):
        """If document links to a file omitted from the manifest, validation fails."""
        manifest_path = ROOT / "sdk/docs/architecture_provenance.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))

        # Remove an important linked file from the manifest
        key_to_remove = next(iter(data["source_files"].keys()))
        del data["source_files"][key_to_remove]

        tampered_manifest = tmp_path / "omitted.json"
        tampered_manifest.write_text(json.dumps(data), encoding="utf-8")

        valid, errors = check_mod.validate_provenance(
            root=ROOT,
            doc_path=Path("sdk/docs/ARCHITECTURE_AND_GUARANTEE_MAP.md"),
            manifest_path=tampered_manifest,
        )
        assert not valid
        expected_msg = f"Architecture map links to unindexed file: {key_to_remove}"
        assert any(expected_msg in e for e in errors)

    def test_strict_mode_detects_hash_drift(self, tmp_path):
        """In strict mode, a tampered/modified file hash must fail validation."""
        manifest_path = ROOT / "sdk/docs/architecture_provenance.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))

        # Tamper with one file hash
        first_key = next(iter(data["source_files"].keys()))
        data["source_files"][first_key] = "0" * 64

        tampered_manifest = tmp_path / "tampered.json"
        tampered_manifest.write_text(json.dumps(data), encoding="utf-8")

        valid, errors = check_mod.validate_provenance(
            root=ROOT,
            manifest_path=tampered_manifest,
            strict=True,
        )
        assert not valid
        assert any(f"Source hash drift detected in {first_key}" in e for e in errors)

    def test_updater_script_generates_valid_manifest(self, tmp_path):
        """The updater script must generate an output that passes verification."""
        generated_manifest = tmp_path / "generated_provenance.json"
        manifest = update_mod.generate_provenance_manifest(
            root=ROOT,
            doc_path=Path("sdk/docs/ARCHITECTURE_AND_GUARANTEE_MAP.md"),
            commit="8c5b4a7",
            output_path=generated_manifest,
        )
        assert manifest["review_base_commit"] == "8c5b4a7"
        assert generated_manifest.exists()

        valid, errors = check_mod.validate_provenance(
            root=ROOT,
            manifest_path=generated_manifest,
            strict=True,
        )
        assert valid, f"Generated manifest failed validation: {errors}"
