#!/usr/bin/env python3
"""Verify that documentation guarantees map to real, existing tests in sdk/tests/.

Part of Issue #150: Machine-verifiable Failure & Threat Model Guarantee-to-Test Map.
Stdlib only: ast, re, pathlib, argparse, sys, dataclasses, time.
"""
from __future__ import annotations

import argparse
import ast
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Tokenizer pattern for test references
# Group 1: filename (e.g. test_storage_backends.py)
# Group 2: optional symbol (e.g. test_redis_storage_atomic_claim or TestClass::test_method)
TEST_REF_PATTERN = re.compile(
    r'(?:tests/)?([a-zA-Z0-9_]+\.py)(?:::([a-zA-Z0-9_]+(?:::[a-zA-Z0-9_]+)*))?'
)

# Section E header pattern (matches Unicode arrow \u2192 or ASCII -> or 'Guarantee')
SECTION_E_HEADER_PATTERN = re.compile(r'^##\s+E\.\s+Guarantee', re.IGNORECASE)
SECTION_NEXT_HEADER_PATTERN = re.compile(r'^##\s+[A-Z]\.', re.IGNORECASE)


@dataclass
class ValidationError:
    """Represents a parsing, syntax, or symbol validation failure."""
    file: Path
    line: int
    message: str
    code: str

    def format_compiler(self, repo_root: Path | None = None) -> str:
        """Format as compiler error: <file>:<line>: error: [<CODE>] <msg>"""
        try:
            rel = self.file.relative_to(repo_root) if repo_root else self.file
        except ValueError:
            rel = self.file
        rel_str = str(rel).replace("\\", "/")
        return f"{rel_str}:{self.line}: error: [{self.code}] {self.message}"

    def format_github(self, repo_root: Path | None = None) -> str:
        """Format as GitHub Actions annotation."""
        try:
            rel = self.file.relative_to(repo_root) if repo_root else self.file
        except ValueError:
            rel = self.file
        rel_str = str(rel).replace("\\", "/")
        return f"::error file={rel_str},line={self.line}::[{self.code}] {self.message}"


@dataclass
class TestReference:
    """A test file and optional symbol extracted from a markdown table cell."""
    raw: str
    file_name: str
    symbol: str | None
    line: int


@dataclass
class GuaranteeRecord:
    """A parsed guarantee row from Section E."""
    id: str
    title: str
    doc_line: int
    where_doc: str
    test_refs: list[TestReference] = field(default_factory=list)
    raw_tests_cell: str = ""


@dataclass
class TestSymbolIndex:
    """Indexed AST symbols for a single test file."""
    file_path: Path
    functions: set[str] = field(default_factory=set)
    classes: set[str] = field(default_factory=set)
    methods: set[str] = field(default_factory=set)
    qualified_methods: set[str] = field(default_factory=set)
    has_test_nodes: bool = False

    @classmethod
    def from_file(cls, path: Path) -> TestSymbolIndex:
        index = cls(file_path=path)
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))

        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.functions.add(node.name)
                if node.name.startswith("test_") or node.name.startswith("test"):
                    index.has_test_nodes = True
            elif isinstance(node, ast.ClassDef):
                index.classes.add(node.name)
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        index.methods.add(item.name)
                        index.qualified_methods.add(f"{node.name}::{item.name}")
                        if item.name.startswith("test_") or item.name.startswith("test"):
                            index.has_test_nodes = True
        return index

    def matches(self, symbol: str | None) -> bool:
        if symbol is None:
            return self.has_test_nodes
        if "::" in symbol:
            return symbol in self.qualified_methods
        return (
            symbol in self.functions
            or symbol in self.methods
            or symbol in self.classes
        )


def _extract_cells(line: str) -> list[str]:
    content = line.strip().removeprefix("|").removesuffix("|")
    return [c.strip() for c in content.split("|")]


def parse_markdown_table(doc_path: Path) -> tuple[list[GuaranteeRecord], list[ValidationError]]:
    """Parse Section E of FAILURE_AND_THREAT_MODEL.md into GuaranteeRecords."""
    records: list[GuaranteeRecord] = []
    errors: list[ValidationError] = []

    if not doc_path.exists() or not doc_path.is_file():
        errors.append(
            ValidationError(
                file=doc_path,
                line=1,
                message=f"Documentation file not found: {doc_path}",
                code="FILE_NOT_FOUND",
            )
        )
        return records, errors

    try:
        lines = doc_path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, OSError) as exc:
        errors.append(
            ValidationError(
                file=doc_path,
                line=1,
                message=f"Failed to read/decode documentation file '{doc_path}': {exc}",
                code="DOC_ENCODING_ERROR",
            )
        )
        return records, errors

    in_section_e = False
    table_header_seen = False
    header_indices: dict[str, int] = {}
    id_col: int | None = None
    guarantee_col: int | None = None
    where_col: int | None = None
    test_col: int | None = None
    seen_ids: dict[str, int] = {}

    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()

        if not in_section_e:
            if SECTION_E_HEADER_PATTERN.search(stripped):
                in_section_e = True
            continue

        # Section E exit condition: encountering next section
        if SECTION_NEXT_HEADER_PATTERN.search(stripped) and not SECTION_E_HEADER_PATTERN.search(stripped):
            break

        # Locate table header
        if not table_header_seen:
            # Case-insensitive header line detection
            if stripped.startswith("|") and any(k in stripped.lower() for k in ("guarantee", "test")):
                cols = _extract_cells(stripped)
                for c_idx, col in enumerate(cols):
                    clean_col = re.sub(r'[*_`#]', '', col).strip().lower()
                    header_indices[clean_col] = c_idx

                def _find_col(
                    candidates: list[str],
                    fuzzy_terms: list[str] | None = None,
                    fallback: int | None = None,
                ) -> int | None:
                    for c in candidates:
                        if c in header_indices:
                            return header_indices[c]
                    if fuzzy_terms:
                        for key, c_i in header_indices.items():
                            if any(term in key for term in fuzzy_terms):
                                return c_i
                    return fallback

                id_col = _find_col(["id", "identifier", "key", "#"], ["id"])
                guarantee_col = _find_col(
                    ["guarantee", "guarantee title", "claim", "title", "promise"],
                    ["guarantee", "claim"],
                    fallback=(1 if id_col == 0 else 0),
                )
                where_col = _find_col(
                    ["where documented", "where", "documented", "documentation", "docs", "spec"],
                    ["where", "doc"],
                    fallback=(2 if len(cols) > 3 else 1),
                )
                test_col = _find_col(
                    ["test(s)", "tests", "test", "test references", "test reference", "test mapping"],
                    ["test"],
                    fallback=len(cols) - 1,
                )
                table_header_seen = True
            continue

        # Skip separator line |---|---|...
        if stripped.startswith("|") and re.match(r"^\|(\s*:?-+:?\s*\|)+$", stripped):
            continue

        # Data rows
        if stripped.startswith("|"):
            cells = _extract_cells(stripped)

            row_test_col = test_col if test_col is not None else len(cells) - 1
            row_guarantee_col = guarantee_col if guarantee_col is not None else 0

            if len(cells) <= max(row_guarantee_col, row_test_col):
                errors.append(
                    ValidationError(
                        file=doc_path,
                        line=idx,
                        message=f"Malformed table row with {len(cells)} columns: {stripped}",
                        code="MALFORMED_ROW",
                    )
                )
                continue

            title = cells[row_guarantee_col]
            where_doc = cells[where_col] if where_col is not None and where_col < len(cells) else ""
            tests_cell = cells[row_test_col]

            gid = cells[id_col] if id_col is not None and id_col < len(cells) else title

            if not gid or not title:
                errors.append(
                    ValidationError(
                        file=doc_path,
                        line=idx,
                        message="Empty guarantee title or identifier",
                        code="EMPTY_TITLE",
                    )
                )
                continue

            if gid in seen_ids:
                errors.append(
                    ValidationError(
                        file=doc_path,
                        line=idx,
                        message=f"Duplicate guarantee identifier '{gid}' (first defined at line {seen_ids[gid]})",
                        code="DUPLICATE_ID",
                    )
                )
            else:
                seen_ids[gid] = idx

            # Strip footnotes (e.g. <sup>1</sup>)
            clean_cell = re.sub(r'<sup>.*?</sup>', '', tests_cell)

            matches = list(TEST_REF_PATTERN.finditer(clean_cell))
            test_refs: list[TestReference] = []

            for m in matches:
                raw = m.group(0)
                file_name = m.group(1)
                symbol = m.group(2)
                rest = clean_cell[m.end():]

                # Check for malformed colon syntax (e.g. :::, single :, or trailing ::)
                if rest.startswith(":"):
                    colon_part = re.match(r"(:+[^\s`\u00b7,\)\(\]\[;]*)", rest)
                    bad_token = raw + (colon_part.group(1) if colon_part else "")
                    errors.append(
                        ValidationError(
                            file=doc_path,
                            line=idx,
                            message=f"Malformed test reference syntax '{bad_token}' (guarantee '{title}')",
                            code="MALFORMED_REFERENCE",
                        )
                    )
                # Check for unconsumed invalid token characters
                elif rest and not re.match(r"^[\s`\u00b7,\)\(\]\[;|^]", rest):
                    tail = re.match(r"([^\s`\u00b7,\)\(\]\[;|^]+)", rest)
                    bad_token = raw + (tail.group(1) if tail else "")
                    errors.append(
                        ValidationError(
                            file=doc_path,
                            line=idx,
                            message=f"Malformed test reference syntax '{bad_token}' (guarantee '{title}')",
                            code="MALFORMED_REFERENCE",
                        )
                    )
                else:
                    test_refs.append(
                        TestReference(
                            raw=raw,
                            file_name=file_name,
                            symbol=symbol,
                            line=idx,
                        )
                    )

            if not test_refs:
                errors.append(
                    ValidationError(
                        file=doc_path,
                        line=idx,
                        message=f"Guarantee '{title}' has no valid test references",
                        code="EMPTY_GUARANTEE",
                    )
                )

            records.append(
                GuaranteeRecord(
                    id=gid,
                    title=title,
                    doc_line=idx,
                    where_doc=where_doc,
                    test_refs=test_refs,
                    raw_tests_cell=tests_cell,
                )
            )

    if not records and not errors:
        errors.append(
            ValidationError(
                file=doc_path,
                line=1,
                message="No Section E guarantee table found in documentation",
                code="SECTION_NOT_FOUND",
            )
        )

    return records, errors


def validate_guarantees(
    records: list[GuaranteeRecord],
    tests_dir: Path,
    doc_path: Path | None = None,
) -> list[ValidationError]:
    """Validate all test references against ASTs in tests_dir."""
    errors: list[ValidationError] = []
    ast_cache: dict[str, TestSymbolIndex | None] = {}

    if not tests_dir.exists() or not tests_dir.is_dir():
        errors.append(
            ValidationError(
                file=tests_dir,
                line=1,
                message=f"Tests directory not found: {tests_dir}",
                code="TESTS_DIR_NOT_FOUND",
            )
        )
        return errors

    for rec in records:
        for ref in rec.test_refs:
            file_path = tests_dir / ref.file_name
            ref_err_file = doc_path if doc_path else file_path

            if not file_path.is_file():
                errors.append(
                    ValidationError(
                        file=ref_err_file,
                        line=ref.line,
                        message=f"Referenced test file does not exist: '{ref.file_name}' (guarantee '{rec.title}')",
                        code="MISSING_FILE",
                    )
                )
                continue

            if ref.file_name not in ast_cache:
                try:
                    ast_cache[ref.file_name] = TestSymbolIndex.from_file(file_path)
                except SyntaxError as exc:
                    ast_cache[ref.file_name] = None
                    errors.append(
                        ValidationError(
                            file=file_path,
                            line=exc.lineno or 1,
                            message=f"Syntax error in test file '{ref.file_name}': {exc.msg}",
                            code="TEST_SYNTAX_ERROR",
                        )
                    )
                    continue
                except (UnicodeDecodeError, OSError) as exc:
                    ast_cache[ref.file_name] = None
                    errors.append(
                        ValidationError(
                            file=file_path,
                            line=1,
                            message=f"Failed to read/decode test file '{ref.file_name}': {exc}",
                            code="TEST_ENCODING_ERROR",
                        )
                    )
                    continue

            index = ast_cache[ref.file_name]
            if index is None:
                continue

            if ref.symbol is None:
                if not index.has_test_nodes:
                    errors.append(
                        ValidationError(
                            file=ref_err_file,
                            line=ref.line,
                            message=f"Test file '{ref.file_name}' contains no test functions or classes (guarantee '{rec.title}')",
                            code="EMPTY_TEST_FILE",
                        )
                    )
            else:
                if not index.matches(ref.symbol):
                    errors.append(
                        ValidationError(
                            file=ref_err_file,
                            line=ref.line,
                            message=f"Referenced test symbol '{ref.symbol}' not found in '{ref.file_name}' (guarantee '{rec.title}')",
                            code="MISSING_SYMBOL",
                        )
                    )

    return errors


def main(argv: list[str] | None = None) -> int:
    repo_root = Path(__file__).resolve().parents[2]
    default_doc = repo_root / "sdk" / "docs" / "FAILURE_AND_THREAT_MODEL.md"
    default_tests = repo_root / "sdk" / "tests"

    parser = argparse.ArgumentParser(
        description="Verify guarantee-to-test mapping in failure and threat model documentation."
    )
    parser.add_argument(
        "--doc",
        type=Path,
        default=default_doc,
        help=f"Path to FAILURE_AND_THREAT_MODEL.md (default: {default_doc})",
    )
    parser.add_argument(
        "--tests-dir",
        type=Path,
        default=default_tests,
        help=f"Path to sdk/tests/ directory (default: {default_tests})",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output",
    )
    parser.add_argument(
        "--github-annotations",
        action="store_true",
        default=os.environ.get("GITHUB_ACTIONS") == "true",
        help="Emit GitHub Actions ::error annotations (default: true if GITHUB_ACTIONS env var set)",
    )

    args = parser.parse_args(argv)

    # CLI / Invocation error checks (exit code 2)
    if not args.doc.exists() or not args.doc.is_file():
        sys.stderr.write(f"Error: documentation file not found or not a regular file: {args.doc}\n")
        return 2
    if not args.tests_dir.exists() or not args.tests_dir.is_dir():
        sys.stderr.write(f"Error: tests directory not found: {args.tests_dir}\n")
        return 2

    records, parse_errors = parse_markdown_table(args.doc)
    val_errors = validate_guarantees(records, args.tests_dir, doc_path=args.doc)
    all_errors = parse_errors + val_errors

    if args.verbose:
        print(f"Parsed {len(records)} guarantees from {args.doc}")
        total_refs = sum(len(r.test_refs) for r in records)
        print(f"Extracted {total_refs} test references across {len({r.file_name for rec in records for r in rec.test_refs})} files")

    if all_errors:
        for err in all_errors:
            print(err.format_compiler(repo_root=repo_root), file=sys.stderr)
            if args.github_annotations:
                print(err.format_github(repo_root=repo_root), file=sys.stderr)
        print(f"\nFAILED: {len(all_errors)} error(s) found in guarantee-to-test mapping.", file=sys.stderr)
        return 1

    total_refs = sum(len(r.test_refs) for r in records)
    distinct_files = {ref.file_name for r in records for ref in r.test_refs}
    print(f"Verified {len(records)} guarantees, {total_refs} test references across {len(distinct_files)} files. 0 errors.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
