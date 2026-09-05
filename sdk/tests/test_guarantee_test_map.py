"""Tests for guarantee-to-test mapping validation engine (Issue #150).

Covers Tiers 1-5 per test methodology:
- Tier 1: Feature Coverage (Markdown parser, AST symbol index, clean validation)
- Tier 2: Boundary & Corner Cases (missing files/symbols, duplicates, footnotes, nested backticks)
- Tier 3: Cross-Feature Combinations (CLI flags, path auto-detection, annotations)
- Tier 4: Real-World Scenarios (repo baseline audit, simulated drift detection)
- Tier 5: Adversarial Hardening (encoding, directory paths, header variants, colons)
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    import types

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / ".github" / "scripts" / "check-guarantee-test-map.py"
REAL_DOC_PATH = REPO_ROOT / "sdk" / "docs" / "FAILURE_AND_THREAT_MODEL.md"
REAL_TESTS_DIR = REPO_ROOT / "sdk" / "tests"


@pytest.fixture(scope="session")
def validator_mod() -> types.ModuleType:
    """Dynamically import .github/scripts/check-guarantee-test-map.py."""
    if not SCRIPT_PATH.is_file():
        pytest.fail(f"Validator script not found at {SCRIPT_PATH}")
    spec = importlib.util.spec_from_file_location("check_guarantee_test_map", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        pytest.fail("Failed to create module spec for validator script")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_guarantee_test_map"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tmp_path():
    """Isolated temporary directory fixture."""
    with tempfile.TemporaryDirectory(prefix="mycelium_test_") as tmp_dir:
        yield Path(tmp_dir)


@pytest.fixture
def make_doc(tmp_path: Path):
    """Generate temporary markdown documents with controlled Section E content."""
    def _create(table_rows: list[str], header_cols: list[str] | None = None) -> Path:
        cols = header_cols or ["Guarantee", "Where documented", "Test(s)"]
        header = "| " + " | ".join(cols) + " |"
        sep = "| " + " | ".join(["---"] * len(cols)) + " |"
        lines = [
            "# Failure & threat model",
            "## C. What we protect",
            "Preamble text.",
            "## E. Guarantee → test map",
            "",
            header,
            sep,
        ]
        lines.extend(table_rows)
        lines.extend(["", "## F. Residual risks", "Residual risks text."])
        path = tmp_path / "TEST_DOC.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path
    return _create


@pytest.fixture
def sample_test_tree(tmp_path: Path) -> Path:
    """Create a controlled directory of test files with various AST structures."""
    t_dir = tmp_path / "tests"
    t_dir.mkdir(parents=True, exist_ok=True)

    # Basic sync & async functions
    (t_dir / "test_basic.py").write_text(
        "def test_sync_one(): pass\n"
        "async def test_async_one(): pass\n"
        "def non_test_helper(): pass\n",
        encoding="utf-8",
    )

    # Classes and class methods
    (t_dir / "test_classes.py").write_text(
        "class TestSuiteAlpha:\n"
        "    def test_alpha_method(self): pass\n"
        "class HelperClass:\n"
        "    def non_test_method(self): pass\n",
        encoding="utf-8",
    )

    # Non-test python file
    (t_dir / "test_empty.py").write_text(
        "CONSTANT = 100\n"
        "def helper(): pass\n",
        encoding="utf-8",
    )

    return t_dir


# ============================================================================
# TIER 1: FEATURE COVERAGE
# ============================================================================

def test_parse_markdown_table_3_columns_clean(validator_mod: Any, make_doc: Any):
    doc = make_doc([
        "| Claim One | README § [Ref](../README.md) | `test_basic.py::test_sync_one` |"
    ])
    records, errors = validator_mod.parse_markdown_table(doc)
    assert len(errors) == 0
    assert len(records) == 1
    assert records[0].title == "Claim One"
    assert records[0].id == "Claim One"
    assert records[0].where_doc == "README § [Ref](../README.md)"
    assert len(records[0].test_refs) == 1
    assert records[0].test_refs[0].file_name == "test_basic.py"
    assert records[0].test_refs[0].symbol == "test_sync_one"


def test_parse_markdown_table_4_columns_id_clean(validator_mod: Any, make_doc: Any):
    doc = make_doc(
        ["| G-01 | Claim One | README | `test_basic.py::test_sync_one` |"],
        header_cols=["ID", "Guarantee", "Where documented", "Test(s)"],
    )
    records, errors = validator_mod.parse_markdown_table(doc)
    assert len(errors) == 0
    assert len(records) == 1
    assert records[0].id == "G-01"
    assert records[0].title == "Claim One"


def test_test_reference_pattern_tokenization(validator_mod: Any):
    raw = (
        "`test_a.py::test_func` · `test_b.py` · `tests/test_c.py::test_other` · "
        "`test_d.py::TestCls::test_m`"
    )
    matches = list(validator_mod.TEST_REF_PATTERN.finditer(raw))
    extracted = [(m.group(1), m.group(2)) for m in matches]
    assert ("test_a.py", "test_func") in extracted
    assert ("test_b.py", None) in extracted
    assert ("test_c.py", "test_other") in extracted
    assert ("test_d.py", "TestCls::test_m") in extracted


def test_symbol_index_top_level_functions(validator_mod: Any, sample_test_tree: Path):
    index = validator_mod.TestSymbolIndex.from_file(sample_test_tree / "test_basic.py")
    assert index.matches("test_sync_one") is True
    assert index.matches("non_test_helper") is True
    assert index.matches("test_ghost") is False


def test_symbol_index_async_functions(validator_mod: Any, sample_test_tree: Path):
    index = validator_mod.TestSymbolIndex.from_file(sample_test_tree / "test_basic.py")
    assert index.matches("test_async_one") is True


def test_symbol_index_class_definitions_and_methods(validator_mod: Any, sample_test_tree: Path):
    index = validator_mod.TestSymbolIndex.from_file(sample_test_tree / "test_classes.py")
    assert index.matches("TestSuiteAlpha") is True
    assert index.matches("TestSuiteAlpha::test_alpha_method") is True


def test_symbol_index_bare_method_resolution(validator_mod: Any, sample_test_tree: Path):
    index = validator_mod.TestSymbolIndex.from_file(sample_test_tree / "test_classes.py")
    assert index.matches("test_alpha_method") is True


def test_symbol_index_whole_file_evaluation(validator_mod: Any, sample_test_tree: Path):
    idx_basic = validator_mod.TestSymbolIndex.from_file(sample_test_tree / "test_basic.py")
    idx_empty = validator_mod.TestSymbolIndex.from_file(sample_test_tree / "test_empty.py")
    assert idx_basic.matches(None) is True
    assert idx_empty.matches(None) is False


def test_validate_guarantees_clean_baseline(
    validator_mod: Any, make_doc: Any, sample_test_tree: Path
):
    doc = make_doc([
        "| Sync Claim | README | `test_basic.py::test_sync_one` |",
        "| Method Claim | README | `test_classes.py::test_alpha_method` |",
        "| Whole File | README | `test_basic.py` |",
    ])
    records, parse_errors = validator_mod.parse_markdown_table(doc)
    assert len(parse_errors) == 0
    val_errors = validator_mod.validate_guarantees(records, sample_test_tree, doc_path=doc)
    assert len(val_errors) == 0


def test_cli_invocation_clean_exit_0(make_doc: Any, sample_test_tree: Path):
    doc = make_doc(["| Valid | README | `test_basic.py::test_sync_one` |"])
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--doc",
            str(doc),
            "--tests-dir",
            str(sample_test_tree),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert "0 errors" in proc.stdout or "Verified" in proc.stdout


# ============================================================================
# TIER 2: BOUNDARY & CORNER CASES
# ============================================================================

def test_error_missing_test_file(validator_mod: Any, make_doc: Any, sample_test_tree: Path):
    doc = make_doc(["| Bad File | README | `test_missing.py::test_sync_one` |"])
    records, _ = validator_mod.parse_markdown_table(doc)
    errors = validator_mod.validate_guarantees(records, sample_test_tree, doc_path=doc)
    assert len(errors) == 1
    assert errors[0].code == "MISSING_FILE"
    assert "test_missing.py" in errors[0].message


def test_error_missing_top_level_function(
    validator_mod: Any, make_doc: Any, sample_test_tree: Path
):
    doc = make_doc(["| Bad Symbol | README | `test_basic.py::test_missing_symbol` |"])
    records, _ = validator_mod.parse_markdown_table(doc)
    errors = validator_mod.validate_guarantees(records, sample_test_tree, doc_path=doc)
    assert len(errors) == 1
    assert errors[0].code == "MISSING_SYMBOL"
    assert "test_missing_symbol" in errors[0].message


def test_error_missing_class_method(validator_mod: Any, make_doc: Any, sample_test_tree: Path):
    doc = make_doc(["| Bad Method | README | `test_classes.py::test_ghost_method` |"])
    records, _ = validator_mod.parse_markdown_table(doc)
    errors = validator_mod.validate_guarantees(records, sample_test_tree, doc_path=doc)
    assert len(errors) == 1
    assert errors[0].code == "MISSING_SYMBOL"


def test_error_duplicate_guarantee_title(validator_mod: Any, make_doc: Any):
    doc = make_doc([
        "| Duplicate Title | README | `test_basic.py::test_sync_one` |",
        "| Duplicate Title | README | `test_classes.py::test_alpha_method` |",
    ])
    _, errors = validator_mod.parse_markdown_table(doc)
    assert any(e.code == "DUPLICATE_ID" for e in errors)


def test_error_duplicate_guarantee_id(validator_mod: Any, make_doc: Any):
    doc = make_doc(
        [
            "| G-01 | Title A | README | `test_basic.py::test_sync_one` |",
            "| G-01 | Title B | README | `test_classes.py::test_alpha_method` |",
        ],
        header_cols=["ID", "Guarantee", "Where documented", "Test(s)"],
    )
    _, errors = validator_mod.parse_markdown_table(doc)
    assert any(e.code == "DUPLICATE_ID" for e in errors)


def test_error_empty_test_cell(validator_mod: Any, make_doc: Any):
    doc = make_doc(["| Empty Cell | README |   |"])
    _, errors = validator_mod.parse_markdown_table(doc)
    assert any(e.code == "EMPTY_GUARANTEE" for e in errors)


def test_error_empty_title_cell(validator_mod: Any, make_doc: Any):
    doc = make_doc(["|  | README | `test_basic.py::test_sync_one` |"])
    _, errors = validator_mod.parse_markdown_table(doc)
    assert any(e.code == "EMPTY_TITLE" for e in errors)


def test_error_malformed_table_row(validator_mod: Any, make_doc: Any):
    doc = make_doc(["| Missing trailing column"])
    _, errors = validator_mod.parse_markdown_table(doc)
    assert any(e.code == "MALFORMED_ROW" for e in errors)


def test_handling_nested_backticks_in_parenthetical(
    validator_mod: Any, make_doc: Any, sample_test_tree: Path
):
    doc = make_doc([
        "| Annotation | README | `test_basic.py::test_sync_one` (asserts `foo`/`bar`) |"
    ])
    records, errors = validator_mod.parse_markdown_table(doc)
    assert len(errors) == 0
    val_errors = validator_mod.validate_guarantees(records, sample_test_tree, doc_path=doc)
    assert len(val_errors) == 0


def test_handling_footnote_superscripts(
    validator_mod: Any, make_doc: Any, sample_test_tree: Path
):
    doc = make_doc(["| Footnote | README | `test_basic.py::test_sync_one`<sup>1</sup> |"])
    records, errors = validator_mod.parse_markdown_table(doc)
    assert len(errors) == 0
    val_errors = validator_mod.validate_guarantees(records, sample_test_tree, doc_path=doc)
    assert len(val_errors) == 0


def test_error_whole_file_empty_test_nodes(
    validator_mod: Any, make_doc: Any, sample_test_tree: Path
):
    doc = make_doc(["| Empty File | README | `test_empty.py` |"])
    records, _ = validator_mod.parse_markdown_table(doc)
    errors = validator_mod.validate_guarantees(records, sample_test_tree, doc_path=doc)
    assert any(e.code == "EMPTY_TEST_FILE" for e in errors)


# ============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS
# ============================================================================

def test_cli_custom_doc_and_tests_dir_flags(make_doc: Any, sample_test_tree: Path):
    doc = make_doc(["| Valid | README | `test_basic.py::test_sync_one` |"])
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--doc",
            str(doc),
            "--tests-dir",
            str(sample_test_tree),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0


def test_cli_path_autodetection_repo_root_and_sdk():
    # From repo root
    proc_root = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc_root.returncode == 0

    # From sdk/
    proc_sdk = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        cwd=REPO_ROOT / "sdk",
        capture_output=True,
        text=True,
    )
    assert proc_sdk.returncode == 0


def test_cli_invalid_arguments_exit_2():
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--doc", "non_existent_file_xyz.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


def test_github_actions_annotation_formatting(validator_mod: Any):
    err = validator_mod.ValidationError(
        file=Path("sdk/docs/FAILURE_AND_THREAT_MODEL.md"),
        line=388,
        message="Simulated error",
        code="TEST_CODE",
    )
    gh = err.format_github(repo_root=REPO_ROOT)
    expected = (
        "::error file=sdk/docs/FAILURE_AND_THREAT_MODEL.md,line=388::[TEST_CODE] Simulated error"
    )
    assert gh == expected
    assert "\\" not in gh


def test_compiler_style_diagnostic_formatting(validator_mod: Any):
    err = validator_mod.ValidationError(
        file=Path("sdk/docs/FAILURE_AND_THREAT_MODEL.md"),
        line=388,
        message="Simulated error",
        code="TEST_CODE",
    )
    comp = err.format_compiler(repo_root=REPO_ROOT)
    assert "sdk/docs/FAILURE_AND_THREAT_MODEL.md:388: error: [TEST_CODE] Simulated error" in comp


# ============================================================================
# TIER 4: REAL-WORLD SCENARIOS
# ============================================================================

def test_real_repo_baseline_passes_all_checks(validator_mod: Any):
    records, parse_errors = validator_mod.parse_markdown_table(REAL_DOC_PATH)
    assert len(parse_errors) == 0
    assert len(records) == 27
    total_refs = sum(len(r.test_refs) for r in records)
    assert total_refs == 108
    distinct_files = {ref.file_name for r in records for ref in r.test_refs}
    assert len(distinct_files) == 36

    val_errors = validator_mod.validate_guarantees(
        records, REAL_TESTS_DIR, doc_path=REAL_DOC_PATH
    )
    assert len(val_errors) == 0


def test_simulated_drift_renamed_or_deleted_function(validator_mod: Any, tmp_path: Path):
    content = REAL_DOC_PATH.read_text(encoding="utf-8")
    assert "test_atomicity_contract.py::test_transition_matrix" in content
    mutated = content.replace(
        "test_atomicity_contract.py::test_transition_matrix",
        "test_atomicity_contract.py::test_renamed_transition_matrix",
    )
    drift_doc = tmp_path / "DRIFT_DOC.md"
    drift_doc.write_text(mutated, encoding="utf-8")

    records, parse_errors = validator_mod.parse_markdown_table(drift_doc)
    assert len(parse_errors) == 0
    val_errors = validator_mod.validate_guarantees(records, REAL_TESTS_DIR, doc_path=drift_doc)
    assert len(val_errors) >= 1
    assert any(
        e.code == "MISSING_SYMBOL" and "test_renamed_transition_matrix" in e.message
        for e in val_errors
    )


def test_simulated_drift_deleted_file(validator_mod: Any, tmp_path: Path):
    content = REAL_DOC_PATH.read_text(encoding="utf-8")
    assert "test_storage_backends.py" in content
    mutated = content.replace("test_storage_backends.py", "test_deleted_storage.py")
    drift_doc = tmp_path / "DRIFT_DOC.md"
    drift_doc.write_text(mutated, encoding="utf-8")

    records, _ = validator_mod.parse_markdown_table(drift_doc)
    val_errors = validator_mod.validate_guarantees(records, REAL_TESTS_DIR, doc_path=drift_doc)
    assert any(
        e.code == "MISSING_FILE" and "test_deleted_storage.py" in e.message
        for e in val_errors
    )


def test_simulated_drift_added_guarantee_without_tests(validator_mod: Any, tmp_path: Path):
    content = REAL_DOC_PATH.read_text(encoding="utf-8")
    lines = content.splitlines()
    insert_idx = next(i for i, line in enumerate(lines) if "## F. Residual risks" in line)
    lines.insert(insert_idx - 1, "| Undocumented Guarantee | README |   |")
    drift_doc = tmp_path / "DRIFT_DOC.md"
    drift_doc.write_text("\n".join(lines), encoding="utf-8")

    _, parse_errors = validator_mod.parse_markdown_table(drift_doc)
    assert any(e.code == "EMPTY_GUARANTEE" for e in parse_errors)


def test_simulated_drift_duplicate_guarantee_title(validator_mod: Any, tmp_path: Path):
    content = REAL_DOC_PATH.read_text(encoding="utf-8")
    lines = content.splitlines()
    insert_idx = next(i for i, line in enumerate(lines) if "## F. Residual risks" in line)
    lines.insert(insert_idx - 1, "| Atomic first claim, single winner | README | `test_basic.py` |")
    drift_doc = tmp_path / "DRIFT_DOC.md"
    drift_doc.write_text("\n".join(lines), encoding="utf-8")

    _, parse_errors = validator_mod.parse_markdown_table(drift_doc)
    assert any(e.code == "DUPLICATE_ID" for e in parse_errors)


# ============================================================================
# TIER 5: ADVERSARIAL HARDENING & REGRESSION SUITE (CHALLENGER 1 FINDINGS)
# ============================================================================

def test_error_non_utf8_test_file(
    validator_mod: Any, make_doc: Any, sample_test_tree: Path
):
    """Verify that a test file with non-UTF8 bytes triggers TEST_ENCODING_ERROR and exits 1."""
    bad_test_file = sample_test_tree / "test_non_utf8.py"
    # Write invalid UTF-8 byte sequences (e.g. latin-1 0xe9, 0xff, 0xfe)
    bad_test_file.write_bytes(b"# Latin-1 / invalid UTF-8: \xe9\xff\xfe\ndef test_broken(): pass\n")

    doc = make_doc(["| Non-UTF8 Test | README | `test_non_utf8.py::test_broken` |"])
    records, parse_errors = validator_mod.parse_markdown_table(doc)
    assert len(parse_errors) == 0

    # 1. API level verification: no unhandled exception, records TEST_ENCODING_ERROR
    val_errors = validator_mod.validate_guarantees(records, sample_test_tree, doc_path=doc)
    assert len(val_errors) >= 1
    encoding_errors = [e for e in val_errors if e.code == "TEST_ENCODING_ERROR"]
    assert len(encoding_errors) == 1
    assert "test_non_utf8.py" in str(encoding_errors[0].file)
    assert any(w in encoding_errors[0].message.lower() for w in ("encoding", "decode", "utf-8"))

    # 2. CLI level verification: exits 1 cleanly without uncaught Python traceback
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--doc",
            str(doc),
            "--tests-dir",
            str(sample_test_tree),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "Traceback (most recent call last)" not in proc.stderr
    assert "TEST_ENCODING_ERROR" in proc.stderr
    assert "FAILED: 1 error(s)" in proc.stderr


def test_error_non_utf8_doc_file(
    validator_mod: Any, tmp_path: Path, sample_test_tree: Path
):
    """Verify that a doc file with non-UTF8 bytes triggers DOC_ENCODING_ERROR and exits 1."""
    doc_path = tmp_path / "NON_UTF8_DOC.md"
    # Write invalid UTF-8 byte sequences in documentation
    doc_path.write_bytes(
        b"# Failure & Threat Model\n"
        b"## E. Guarantee \xe9\xff\xfe\n"
        b"| ID | Guarantee | Where | Test(s) |\n"
        b"|---|---|---|---|\n"
        b"| G1 | Corrupt claim | doc | `test_basic.py::test_sync_one` |\n"
    )

    # 1. API level verification: no unhandled exception, records DOC_ENCODING_ERROR
    records, parse_errors = validator_mod.parse_markdown_table(doc_path)
    assert len(records) == 0
    assert len(parse_errors) >= 1
    doc_errors = [e for e in parse_errors if e.code == "DOC_ENCODING_ERROR"]
    assert len(doc_errors) == 1
    assert doc_errors[0].file == doc_path
    assert doc_errors[0].line == 1
    assert any(w in doc_errors[0].message.lower() for w in ("encoding", "decode", "utf-8"))

    # 2. CLI level verification: exits 1 cleanly without uncaught Python traceback
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--doc",
            str(doc_path),
            "--tests-dir",
            str(sample_test_tree),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "Traceback (most recent call last)" not in proc.stderr
    assert "DOC_ENCODING_ERROR" in proc.stderr
    assert "FAILED: 1 error(s)" in proc.stderr


def test_cli_directory_passed_as_doc_exits_2(
    validator_mod: Any, tmp_path: Path, sample_test_tree: Path
):
    """Verify that passing a directory to --doc returns exit code 2 cleanly with an error."""
    doc_dir = tmp_path / "fake_doc_directory"
    doc_dir.mkdir(parents=True, exist_ok=True)

    # 1. CLI verification: exit code 2, no PermissionError, IsADirectoryError, or traceback
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--doc",
            str(doc_dir),
            "--tests-dir",
            str(sample_test_tree),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "PermissionError" not in proc.stderr
    assert "IsADirectoryError" not in proc.stderr
    assert "Traceback (most recent call last)" not in proc.stderr
    assert "Error:" in proc.stderr or "documentation file" in proc.stderr.lower()

    # 2. API level verification: parse_markdown_table handles directory path without crashing
    records, errors = validator_mod.parse_markdown_table(doc_dir)
    assert len(records) == 0
    assert any(e.code == "FILE_NOT_FOUND" for e in errors)


def test_header_tests_plural_variants(
    validator_mod: Any, make_doc: Any, sample_test_tree: Path
):
    """Verify table headers with Tests variants resolve test column in 3-col and 4-col tables."""
    header_variants = [
        ["ID", "Guarantee", "Where documented", "Tests"],
        ["ID", "Guarantee", "Where documented", "Test"],
        ["ID", "Guarantee", "Where documented", "Test(s)"],
        ["Guarantee", "Where documented", "Tests"],
        ["Guarantee", "Where documented", "Test"],
        ["Guarantee", "Where documented", "Test(s)"],
    ]

    for header_cols in header_variants:
        if len(header_cols) == 4:
            row = "| G-01 | Guarantee One | README.md § 1.2 | `test_basic.py::test_sync_one` |"
        else:
            row = "| Guarantee One | README.md § 1.2 | `test_basic.py::test_sync_one` |"

        doc = make_doc([row], header_cols=header_cols)
        records, parse_errors = validator_mod.parse_markdown_table(doc)

        msg = f"Unexpected parse errors for header {header_cols}: {parse_errors}"
        assert len(parse_errors) == 0, msg
        assert len(records) == 1
        assert len(records[0].test_refs) == 1
        assert records[0].test_refs[0].file_name == "test_basic.py"
        assert records[0].test_refs[0].symbol == "test_sync_one"

        val_errors = validator_mod.validate_guarantees(records, sample_test_tree, doc_path=doc)
        assert len(val_errors) == 0, f"Unexpected validation errors: {val_errors}"

        # Also verify via CLI invocation
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--doc",
                str(doc),
                "--tests-dir",
                str(sample_test_tree),
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0
        assert "0 errors" in proc.stdout


def test_error_malformed_colon_syntax(
    validator_mod: Any, make_doc: Any, sample_test_tree: Path
):
    """Verify references with malformed colon syntax are rejected and not silently accepted."""
    bad_references = [
        "test_basic.py:::test_sync_one",       # triple colons typo
        "test_basic.py::::test_sync_one",      # quadruple colons typo
        "test_basic.py:test_sync_one",         # single colon typo
        "test_basic.py::",                     # trailing double colon without symbol
        "test_basic.py:::",                    # trailing triple colon
    ]

    for bad_reference in bad_references:
        doc = make_doc([f"| Claim | README | `{bad_reference}` |"])
        records, parse_errors = validator_mod.parse_markdown_table(doc)

        # Must produce an error at parse time or validation time
        if parse_errors:
            assert any(
                e.code in ("MALFORMED_REFERENCE", "INVALID_TEST_REFERENCE", "EMPTY_GUARANTEE")
                for e in parse_errors
            )
        else:
            val_errors = validator_mod.validate_guarantees(records, sample_test_tree, doc_path=doc)
            assert len(val_errors) >= 1
            assert any(
                e.code in ("MALFORMED_REFERENCE", "INVALID_TEST_REFERENCE", "MISSING_SYMBOL")
                for e in val_errors
            )

        # CLI verification: must exit 1 and report failure
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT_PATH),
                "--doc",
                str(doc),
                "--tests-dir",
                str(sample_test_tree),
            ],
            capture_output=True,
            text=True,
        )
        err_msg = (
            f"Expected CLI exit 1 for malformed reference '{bad_reference}', got {proc.returncode}"
        )
        assert proc.returncode == 1, err_msg
        assert "FAILED:" in proc.stderr
        assert "0 errors" not in proc.stdout


