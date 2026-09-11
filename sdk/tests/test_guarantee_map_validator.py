"""Tests for guarantee-to-test map machine verification and drift detection."""

import ast
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = ROOT / ".github" / "scripts"
SCRIPT_PATH = SCRIPTS_DIR / "check-guarantee-map.py"


@pytest.fixture
def tmp_path():
    """Isolated temporary directory fixture safe from Windows permission locks on pytest-of-user."""
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


def _load_script_module(name: str, filename: str):
    """Dynamically import helper script from .github/scripts/ without sys.path pollution."""
    script_path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(name, script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


validator_mod = _load_script_module("check_guarantee_map", "check-guarantee-map.py")


class TestGuaranteeMapValidator:
    """Comprehensive test suite for the guarantee-to-test map validator."""

    def test_live_guarantee_map_passes(self):
        """The checked-in FAILURE_AND_THREAT_MODEL.md must validate cleanly with 0 errors."""
        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=Path("sdk/docs/FAILURE_AND_THREAT_MODEL.md"),
        )
        assert valid, f"Guarantee map validation failed: {errors}"
        assert len(errors) == 0

    def test_live_guarantee_map_covers_all_shipped_guarantees(self):
        """Section E must parse all 27 shipped guarantee rows with non-empty test links."""
        doc_path = ROOT / "sdk/docs/FAILURE_AND_THREAT_MODEL.md"
        content = doc_path.read_text(encoding="utf-8")
        rows, errors = validator_mod.parse_guarantee_map(content, doc_path)

        assert len(errors) == 0
        assert len(rows) >= 27
        for row in rows:
            assert len(row.identifier) > 0
            assert len(row.test_refs) >= 1
            for ref in row.test_refs:
                assert ref.file_path_str.endswith(".py")

    def test_cli_live_guarantee_map_exit_code_zero(self):
        """Executing check-guarantee-map.py directly via CLI must exit with returncode 0."""
        result = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--root", str(ROOT)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"CLI exited with {result.returncode}.\n"
            f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )
        assert "Guarantee map verified successfully" in result.stdout

    def test_detects_nonexistent_test_file(self, tmp_path):
        """Validator must detect and fail on non-existent test files."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Valid Claim | README | `test_version.py::test_version_matches_pyproject` |
| Phantom Claim | README | `test_nonexistent_file_xyz_123.py::test_phantom` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any(
            "references non-existent test file: 'test_nonexistent_file_xyz_123.py'" in e
            for e in errors
        )

    def test_cli_detects_nonexistent_test_file_exit_code_one(self, tmp_path):
        """CLI invocation on doc with missing test file must exit with returncode 1."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Broken File | README | `test_completely_missing_xyz.py::test_missing` |
""",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--root",
                str(ROOT),
                "--doc",
                str(synthetic_doc),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "non-existent test file" in result.stderr

    def test_detects_nonexistent_test_symbol(self, tmp_path):
        """Validator must detect and fail on non-existent test functions within existing files."""
        synthetic_test_dir = tmp_path / "tests"
        synthetic_test_dir.mkdir()
        test_file = synthetic_test_dir / "test_sample.py"
        test_file.write_text(
            """
def test_real_one():
    assert True
""",
            encoding="utf-8",
        )

        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Real Test | README | `test_sample.py::test_real_one` |
| Fake Test | README | `test_sample.py::test_does_not_exist` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=tmp_path,
            doc_path=synthetic_doc,
            test_dir=synthetic_test_dir,
        )
        assert not valid
        assert any(
            "references non-existent test symbol 'test_does_not_exist' in 'test_sample.py'" in e
            for e in errors
        )

    def test_cli_detects_nonexistent_test_symbol_exit_code_one(self, tmp_path):
        """CLI invocation on doc with missing test symbol must exit with returncode 1."""
        synthetic_test_dir = tmp_path / "tests"
        synthetic_test_dir.mkdir()
        test_file = synthetic_test_dir / "test_sample.py"
        test_file.write_text("def test_real(): pass\n", encoding="utf-8")

        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Bad Symbol | README | `test_sample.py::test_missing_func` |
""",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--root",
                str(tmp_path),
                "--doc",
                str(synthetic_doc),
                "--test-dir",
                str(synthetic_test_dir),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "non-existent test symbol 'test_missing_func'" in result.stderr

    def test_detects_duplicate_guarantee_identifier(self, tmp_path):
        """Validator must detect and reject duplicate guarantee identifiers."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Atomic Claim | § 1 | `test_version.py::test_version_matches_pyproject` |
| Other Guarantee | § 2 | `test_version.py::test_version_matches_pyproject` |
| Atomic Claim | § 3 | `test_version.py::test_version_matches_pyproject` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any(
            "Duplicate guarantee identifier 'Atomic Claim' at line 8 (first defined at line 6)" in e
            for e in errors
        )

    def test_cli_detects_duplicate_guarantee_exit_code_one(self, tmp_path):
        """CLI invocation on doc with duplicate guarantee identifier must exit with code 1."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Duplicated ID | § 1 | `test_version.py::test_version_matches_pyproject` |
| Duplicated ID | § 2 | `test_version.py::test_version_matches_pyproject` |
""",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--root",
                str(ROOT),
                "--doc",
                str(synthetic_doc),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "Duplicate guarantee identifier 'Duplicated ID'" in result.stderr

    def test_detects_empty_or_malformed_identifier(self, tmp_path):
        """Empty or punctuation-only guarantee identifiers must be rejected."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
|   | README | `test_storage_backends.py::test_file_storage_serializes_concurrent_claims` |
| !!! | README | `test_storage_backends.py::test_file_storage_serializes_concurrent_claims` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any("Empty guarantee identifier" in e for e in errors)
        assert any("must contain alphanumeric characters" in e for e in errors)

    def test_detects_missing_section_e(self, tmp_path):
        """Missing Section E in documentation must fail with clean error."""
        synthetic_doc = tmp_path / "NO_SECTION_E.md"
        synthetic_doc.write_text("# Model\nSome text without section E.\n", encoding="utf-8")

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any("Section '## E. Guarantee → test map' not found" in e for e in errors)

    def test_detects_missing_table_in_section_e(self, tmp_path):
        """Section E without markdown table rows must fail with clean error."""
        synthetic_doc = tmp_path / "NO_TABLE.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

This section contains text but no markdown table.

## F. Residual risks
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any("No markdown table found in Section E" in e for e in errors)

    def test_detects_guarantee_row_without_test_references(self, tmp_path):
        """Row lacking valid test references must fail validation."""
        synthetic_doc = tmp_path / "EMPTY_TESTS.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Empty Test Row | README | TBD / None |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any("has no valid test references" in e for e in errors)

    def test_ast_symbol_extraction_hierarchy(self):
        """AST extractor must collect functions, classes, and class methods."""
        code = """
def top_level_sync():
    pass

async def top_level_async():
    pass

class TestSuiteAlpha:
    def method_one(self):
        pass

    async def async_method_two(self):
        pass

    class NestedHelper:
        def nested_method(self):
            pass
"""
        tree = ast.parse(code)
        symbols = validator_mod.extract_symbols_from_ast(tree)

        assert "top_level_sync" in symbols
        assert "top_level_async" in symbols
        assert "TestSuiteAlpha" in symbols
        assert "method_one" in symbols
        assert "TestSuiteAlpha::method_one" in symbols
        assert "async_method_two" in symbols
        assert "TestSuiteAlpha::async_method_two" in symbols
        assert "NestedHelper" in symbols
        assert "TestSuiteAlpha::NestedHelper" in symbols
        assert "nested_method" in symbols
        assert "NestedHelper::nested_method" in symbols

    def test_syntax_error_in_test_file_detected(self, tmp_path):
        """Syntax error in a referenced test file must fail validation with diagnostic."""
        bad_test_dir = tmp_path / "tests"
        bad_test_dir.mkdir()
        bad_file = bad_test_dir / "test_syntax_err.py"
        bad_file.write_text("def broken_syntax(:\n", encoding="utf-8")

        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Syntax Guarantee | README | `test_syntax_err.py::test_broken` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=tmp_path,
            doc_path=synthetic_doc,
            test_dir=bad_test_dir,
        )
        assert not valid
        assert any("SyntaxError parsing test file" in e for e in errors)

    def test_missing_doc_file_detected(self, tmp_path):
        """Non-existent documentation path fails gracefully."""
        nonexistent = tmp_path / "missing_file.md"
        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=nonexistent,
        )
        assert not valid
        assert any("Documentation file not found" in e for e in errors)

    def test_id_column_supported_when_present(self, tmp_path):
        """Tables with explicit ID columns use ID for uniqueness."""
        synthetic_doc = tmp_path / "ID_COL_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| ID | Guarantee | Where documented | Test(s) |
|---|---|---|---|
| G1 | First | README | `test_version.py::test_version_matches_pyproject` |
| G2 | Second | README | `test_version.py::test_version_matches_pyproject` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert valid, f"Expected clean pass with ID column: {errors}"

    def test_detects_nonexistent_class_qualifier(self, tmp_path):
        """A test reference with a bogus class prefix must be rejected even if function exists."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Bogus Class Claim | README | `test_version.py::BogusClass::test_version_matches_pyproject` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any(
            "references non-existent test symbol 'BogusClass::test_version_matches_pyproject'" in e
            for e in errors
        )

    def test_detects_mismatched_class_method(self, tmp_path):
        """A method in ClassB cited under ClassA must fail validation."""
        synthetic_test_dir = tmp_path / "tests"
        synthetic_test_dir.mkdir()
        test_file = synthetic_test_dir / "test_sample.py"
        test_file.write_text(
            """
class ClassA:
    def method_a(self): pass

class ClassB:
    def method_b(self): pass
""",
            encoding="utf-8",
        )

        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Mismatched Method | README | `test_sample.py::ClassA::method_b` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=tmp_path,
            doc_path=synthetic_doc,
            test_dir=synthetic_test_dir,
        )
        assert not valid
        assert any("references non-existent test symbol 'ClassA::method_b'" in e for e in errors)

    def test_detects_inner_helper_closure_rejected(self, tmp_path):
        """Functions defined inside other functions are closures, not runnable pytest tests."""
        synthetic_test_dir = tmp_path / "tests"
        synthetic_test_dir.mkdir()
        test_file = synthetic_test_dir / "test_sample.py"
        test_file.write_text(
            """
def test_real():
    def inner_helper():
        pass
""",
            encoding="utf-8",
        )

        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Inner Closure | README | `test_sample.py::inner_helper` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=tmp_path,
            doc_path=synthetic_doc,
            test_dir=synthetic_test_dir,
        )
        assert not valid
        assert any("references non-existent test symbol 'inner_helper'" in e for e in errors)

    def test_table_row_with_escaped_pipes_and_code_spans(self, tmp_path):
        """Escaped pipes \\| and code spans with pipes must not break cell splitting."""
        synthetic_doc = tmp_path / "ESCAPED_PIPES.md"
        claim_tests = "`test_version.py::test_version_matches_pyproject` (with `flag | opt`)"
        synthetic_doc.write_text(
            f"""# Model
## E. Guarantee → test map

| Guarantee \\| Feature A | README § [Gates \\| Backends](link) | {claim_tests} |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert valid, f"Escaped pipes and code spans should parse cleanly: {errors}"

    def test_markdown_link_url_test_reference(self, tmp_path):
        """Test references formatted as markdown link targets must be parsed and verified."""
        synthetic_doc = tmp_path / "LINK_TARGET.md"
        link_ref = (
            "[Click here to inspect test](test_version.py::test_version_matches_pyproject#L10)"
        )
        synthetic_doc.write_text(
            f"""# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Link Target Claim | README | {link_ref} |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert valid, f"Expected markdown link target to resolve: {errors}"

    def test_detects_case_and_whitespace_duplicate_guarantees(self, tmp_path):
        """Duplicate identifiers differing only by casing or internal whitespace must be caught."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Atomic Guarantee | § 1 | `test_version.py::test_version_matches_pyproject` |
| atomic   guarantee | § 2 | `test_version.py::test_version_matches_pyproject` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        expected_msg = (
            "Duplicate guarantee identifier 'atomic guarantee' at line 7 "
            "(first defined as 'Atomic Guarantee' at line 6)"
        )
        assert any(expected_msg in e for e in errors)

    def test_case_sensitive_test_filename_enforced(self, tmp_path):
        """Referencing a test file with incorrect casing must fail validation for safety."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Bad Case | README | `TEST_VERSION.py::test_version_matches_pyproject` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any("references non-existent test file: 'TEST_VERSION.py'" in e for e in errors)

    def test_non_utf8_test_file_handled_gracefully(self, tmp_path):
        """Non-UTF-8 test files must produce a diagnostic error rather than crashing."""
        synthetic_test_dir = tmp_path / "tests"
        synthetic_test_dir.mkdir()
        bad_file = synthetic_test_dir / "test_invalid_encoding.py"
        # Write invalid UTF-8 bytes
        bad_file.write_bytes(b"\xff\xfe\x00\x00 invalid bytes")

        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Encoding Claim | README | `test_invalid_encoding.py::test_dummy` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=tmp_path,
            doc_path=synthetic_doc,
            test_dir=synthetic_test_dir,
        )
        assert not valid
        assert any("Error reading test file" in e for e in errors)

    def test_detects_nonexistent_directory_for_existing_filename(self, tmp_path):
        """Citing a non-existent subdirectory for an existing filename must fail validation."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Bad Dir | README | `bogus_folder/subfolder/test_version.py` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any(
            "references non-existent test file: 'bogus_folder/subfolder/test_version.py'" in e
            for e in errors
        )

    def test_case_sensitive_directory_components_enforced(self, tmp_path):
        """Referencing a test path with incorrect directory casing must fail validation."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Bad Dir Case | README | `SDK/tests/test_version.py::test_version_matches_pyproject` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any(
            "references non-existent test file: 'SDK/tests/test_version.py'" in e for e in errors
        )

    def test_external_url_not_treated_as_test_reference(self, tmp_path):
        """External URLs must not be parsed as test files or trigger network calls."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        url = "https://github.com/mycelium-labs/mycelium/blob/main/sdk/tests/test_version.py"
        synthetic_doc.write_text(
            f"""# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| External Link | README | `test_version.py` (view [online]({url})) |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert valid, f"External URL link should not interfere with test resolution: {errors}"

    def test_ast_discovers_symbols_in_compound_statements(self, tmp_path):
        """AST extractor must discover tests defined inside if/try compound blocks."""
        synthetic_test_dir = tmp_path / "tests"
        synthetic_test_dir.mkdir()
        test_file = synthetic_test_dir / "test_compound.py"
        test_file.write_text(
            """
if True:
    def test_conditional():
        pass

try:
    def test_in_try():
        pass
except Exception:
    pass

class TestCompoundSuite:
    if True:
        def test_method_in_if(self):
            pass
""",
            encoding="utf-8",
        )

        tree = ast.parse(test_file.read_text(encoding="utf-8"))
        symbols = validator_mod.extract_symbols_from_ast(tree)

        assert "test_conditional" in symbols
        assert "test_in_try" in symbols
        assert "TestCompoundSuite::test_method_in_if" in symbols

        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Cond 1 | README | `test_compound.py::test_conditional` |
| Cond 2 | README | `test_compound.py::test_in_try` |
| Cond 3 | README | `test_compound.py::TestCompoundSuite::test_method_in_if` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=tmp_path,
            doc_path=synthetic_doc,
            test_dir=synthetic_test_dir,
        )
        assert valid, f"Symbols in compound statements should be discovered: {errors}"

    def test_path_traversal_test_file_rejected(self, tmp_path):
        """Directory traversal escaping the repository root must fail validation."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Traversal Claim | README | `../../outside_test.py::test_escape` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any(
            "references non-existent test file: '../../outside_test.py'" in e for e in errors
        )

    def test_multi_line_html_comment_in_section_e(self, tmp_path):
        """Multi-line HTML comments in Section E must not terminate table parsing."""
        synthetic_doc = tmp_path / "SYNTHETIC_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee \u2192 test map

<!--
Multi-line comment explaining the table
with multiple lines of text
-->

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Comment Claim | README | `test_version.py::test_version_matches_pyproject` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert valid, f"Multi-line HTML comment should not break table parsing: {errors}"

    def test_table_in_subsequent_section_not_parsed_as_section_e(self, tmp_path):
        """A table in Section F must never leak into Section E when Section E has no table."""
        synthetic_doc = tmp_path / "LEAK_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee → test map

Section E has discussion text but no markdown table.

---

## F. Residual risks

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Rogue Claim | README | `test_version.py::test_version_matches_pyproject` |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any("No markdown table found in Section E" in e for e in errors)
        assert not any("Rogue Claim" in e for e in errors)

    def test_empty_table_row_detected_as_error(self, tmp_path):
        """A table row with all empty cells must not be silently skipped as a delimiter."""
        synthetic_doc = tmp_path / "EMPTY_ROW_MODEL.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee → test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Valid Claim | README | `test_version.py::test_version_matches_pyproject` |
| | | |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any("Empty guarantee identifier" in e for e in errors)

    def test_detects_malformed_test_references(self, tmp_path):
        """Malformed test references (bad colons, bad extensions, bad symbols) must fail."""
        malformed_samples = [
            ("Dangling Colon", "`test_version.py::`", "invalid test symbol identifier"),
            ("Triple Colon", "`test_version.py:::test`", "invalid test symbol identifier"),
            ("Compiled File", "`test_version.pyc`", "test file path must end with '.py'"),
            ("Backup Extension", "`test_version.py.bak`", "test file path must end with '.py'"),
            (
                "Bad Symbol Ext",
                "`test_version.py::test_func.bak`",
                "invalid test symbol identifier",
            ),
            (
                "Bad Symbol Identifier",
                "`test_version.py::123bad`",
                "invalid test symbol identifier",
            ),
            ("Empty Filename", "`tests/.py::test_func`", "empty test filename"),
        ]

        for claim_name, bad_ref, expected_reason in malformed_samples:
            synthetic_doc = tmp_path / f"MALFORMED_{claim_name.replace(' ', '_')}.md"
            synthetic_doc.write_text(
                f"""# Model
## E. Guarantee → test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| {claim_name} | README | {bad_ref} |
""",
                encoding="utf-8",
            )

            valid, errors = validator_mod.validate_guarantee_map(
                root=ROOT,
                doc_path=synthetic_doc,
            )
            assert not valid, f"Expected validation failure for {claim_name} ({bad_ref})"
            assert any(
                "has malformed test reference" in e and expected_reason in e for e in errors
            ), f"Expected reason '{expected_reason}' in errors: {errors}"

    def test_cli_detects_malformed_test_reference_exit_code_one(self, tmp_path):
        """CLI execution on document with malformed test reference exits with code 1."""
        synthetic_doc = tmp_path / "SYNTHETIC_MALFORMED.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee → test map

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Bad Ref | README | `test_version.py::` |
""",
            encoding="utf-8",
        )

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--root",
                str(ROOT),
                "--doc",
                str(synthetic_doc),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 1
        assert "malformed test reference 'test_version.py::'" in result.stderr

    def test_detects_indistinguishable_header_columns(self, tmp_path):
        """Table header where id and test columns resolve to same index must fail."""
        synthetic_doc = tmp_path / "BAD_HEADER.md"
        synthetic_doc.write_text(
            """# Model
## E. Guarantee → test map

| Guarantee Tests | Notes |
|---|---|
| `test_version.py` | note |
""",
            encoding="utf-8",
        )

        valid, errors = validator_mod.validate_guarantee_map(
            root=ROOT,
            doc_path=synthetic_doc,
        )
        assert not valid
        assert any(
            "Malformed table header" in e
            and "could not distinguish guarantee identifier and test columns" in e
            for e in errors
        )

    def test_path_with_backslashes_normalized(self):
        """Paths specified with Windows-style backslashes normalize and resolve cleanly."""
        resolved = validator_mod.resolve_test_file(
            ROOT, "sdk\\tests\\test_version.py", ROOT / "sdk" / "tests"
        )
        assert resolved is not None
        assert resolved.name == "test_version.py"
