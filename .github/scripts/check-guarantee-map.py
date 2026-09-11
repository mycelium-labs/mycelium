#!/usr/bin/env python3
"""Validate that the guarantee-to-test map in FAILURE_AND_THREAT_MODEL.md is accurate and non-drifting.

Verifies:
1. Section E ('## E. Guarantee -> test map') exists and contains a valid markdown table.
2. Every guarantee identifier is unique, non-empty, and correctly formatted.
3. Every referenced test file exists in sdk/tests/ or repository test directories.
4. Every referenced test function or test class actually exists within the target test file (verified via AST parsing).
5. Any duplicate, missing, or malformed entries fail execution with a descriptive error report and non-zero exit code.
6. Emits compiler diagnostics and optional GitHub Actions annotations on failure.

Usage:
    python .github/scripts/check-guarantee-map.py [--root <DIR>] [--doc <DOC>] [--test-dir <TEST_DIR>]
"""

import argparse
import ast
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Ensure stdout and stderr handle unicode characters safely across all platforms (e.g. Windows cp1252)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

DEFAULT_DOC = "sdk/docs/FAILURE_AND_THREAT_MODEL.md"
DEFAULT_TEST_DIR = "sdk/tests"

# Pattern to locate Section E heading (supporting ->, →, –, —, or "to")
SECTION_E_PATTERN = re.compile(
    r"(?m)^##\s+E\.\s+Guarantee\s*(?:[\u2192\u2013\u2014\->]+|to)\s*test\s+map\s*$",
    re.IGNORECASE,
)

# Pattern to extract test references ending in .py, optionally followed by ::symbol.
# Lookbehind includes ':' to avoid matching URL schemes like http://... or https://...
TEST_REF_PATTERN = re.compile(
    r"(?<![\w/\\.:-])([a-zA-Z0-9_\/\\.-]+\.py(?:::[a-zA-Z0-9_]+)*)"
)


class SymbolCollection(set[str]):
    """Set of runnable test symbols with separated qualified and bare lookup sets."""

    def __init__(self, qualified: Iterable[str] = (), bare: Iterable[str] = ()) -> None:
        super().__init__(set(qualified) | set(bare))
        self.qualified: set[str] = set(qualified)
        self.bare: set[str] = set(bare)


@dataclass
class TestReference:
    raw: str
    file_path_str: str
    symbols: list[str]
    resolved_path: Path | None = None
    is_malformed: bool = False
    malformed_reason: str = ""


@dataclass
class GuaranteeRow:
    identifier: str
    doc_ref: str
    tests_raw: str
    test_refs: list[TestReference]
    line_number: int


def emit_error(message: str, file: str | None = None, line: int | None = None) -> None:
    """Print an error and optional GitHub Actions annotation."""
    loc = ""
    if file:
        loc = f"{file}:{line or 1}: "
    print(f"{loc}error: {message}", file=sys.stderr)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        annotation = f"::error file={file or 'guarantee-map'}"
        if line:
            annotation += f",line={line}"
        annotation += f"::{message}"
        print(annotation, file=sys.stderr)


def split_markdown_table_row(line: str) -> list[str]:
    """Split a markdown table row into cells, respecting escaped pipes and code spans."""
    stripped = line.strip()
    if not stripped:
        return []

    # Strip optional outer pipes if present and unescaped
    stripped = stripped.removeprefix("|")
    if stripped.endswith("|"):
        # Count preceding backslashes to ensure trailing pipe is not escaped (\|)
        num_bs = 0
        pos = len(stripped) - 2
        while pos >= 0 and stripped[pos] == "\\":
            num_bs += 1
            pos -= 1
        if num_bs % 2 == 0:
            stripped = stripped[:-1]

    cells: list[str] = []
    current_cell: list[str] = []
    idx = 0
    in_code = False
    code_delim_len = 0
    n = len(stripped)

    while idx < n:
        char = stripped[idx]

        # Handle escaped characters like \| or \`
        if char == "\\" and idx + 1 < n:
            next_char = stripped[idx + 1]
            if next_char == "|":
                current_cell.append("|")
                idx += 2
                continue
            else:
                current_cell.append(char)
                current_cell.append(next_char)
                idx += 2
                continue

        # Handle backtick code spans
        if char == "`":
            run_len = 1
            while idx + run_len < n and stripped[idx + run_len] == "`":
                run_len += 1
            if not in_code:
                in_code = True
                code_delim_len = run_len
            elif run_len == code_delim_len:
                in_code = False
                code_delim_len = 0
            current_cell.append(stripped[idx : idx + run_len])
            idx += run_len
            continue

        # Handle unescaped pipe outside code spans
        if char == "|" and not in_code:
            cells.append("".join(current_cell).strip())
            current_cell = []
            idx += 1
            continue

        current_cell.append(char)
        idx += 1

    cells.append("".join(current_cell).strip())
    return cells


def extract_symbols_from_ast(tree: ast.AST) -> SymbolCollection:
    """Extract top-level functions, classes, and class methods from an AST.

    Supports test symbols defined conditionally inside compound statements
    (e.g. if/try blocks). Inner helper functions or local closures defined
    inside function bodies are excluded because pytest cannot discover or
    address them directly.
    """
    qualified_symbols: set[str] = set()
    bare_symbols: set[str] = set()

    class SymbolVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.class_stack: list[str] = []

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            qualified = "::".join(self.class_stack + [node.name])
            qualified_symbols.add(qualified)
            qualified_symbols.add(node.name)
            bare_symbols.add(node.name)

            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            bare_symbols.add(node.name)
            if self.class_stack:
                qualified = "::".join(self.class_stack + [node.name])
                qualified_symbols.add(qualified)
                if len(self.class_stack) > 1:
                    qualified_symbols.add(f"{self.class_stack[-1]}::{node.name}")
            else:
                qualified_symbols.add(node.name)
            # Deliberately do NOT recurse into function bodies: inner functions/closures
            # are not pytest test items.

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            bare_symbols.add(node.name)
            if self.class_stack:
                qualified = "::".join(self.class_stack + [node.name])
                qualified_symbols.add(qualified)
                if len(self.class_stack) > 1:
                    qualified_symbols.add(f"{self.class_stack[-1]}::{node.name}")
            else:
                qualified_symbols.add(node.name)
            # Deliberately do NOT recurse into function bodies

    SymbolVisitor().visit(tree)

    return SymbolCollection(qualified_symbols, bare_symbols)


def parse_test_references(cell_text: str) -> list[TestReference]:
    """Extract and validate test references from a markdown table cell."""
    # Strip HTML comments
    cleaned = re.sub(r"<!--.*?-->", "", cell_text, flags=re.DOTALL)
    # Strip HTML tags such as <sup>1</sup>
    cleaned = re.sub(r"<[^>]+>", "", cleaned)

    # Handle markdown links: [anchor](url)
    # If anchor contains .py, keep anchor.
    # Otherwise if url contains .py (and is not an external URL), extract from url.
    def replace_link(match: re.Match[str]) -> str:
        anchor = match.group(1)
        url = match.group(2).strip()
        # External URLs should never be extracted as local test files
        if re.match(r"^[a-zA-Z]+://", url) or url.startswith("//"):
            return anchor
        if ".py" in anchor:
            return anchor
        if ".py" in url:
            url_clean = url.split("#")[0].split("?")[0]
            return f" {url_clean} "
        return anchor

    cleaned = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", replace_link, cleaned)

    candidate_pattern = re.compile(
        r"(?<![\w/\\.:-])([a-zA-Z0-9_\/\\.:-]*\.py[a-zA-Z0-9_\/\\.:-]*)"
    )
    matches = candidate_pattern.findall(cleaned)
    refs: list[TestReference] = []
    seen_raw: set[str] = set()

    for m in matches:
        cand = m.strip()
        # Skip external URLs or UNC network paths
        if "://" in cand or cand.startswith(("//", "\\\\")):
            continue

        # Strip sentence-ending punctuation like period, comma, or semicolon
        raw_token = cand.rstrip(".,;")
        if not raw_token or raw_token in seen_raw:
            continue
        seen_raw.add(raw_token)

        parts = raw_token.split("::")
        file_part = parts[0]

        if not file_part.endswith(".py"):
            refs.append(
                TestReference(
                    raw=raw_token,
                    file_path_str=file_part,
                    symbols=[],
                    is_malformed=True,
                    malformed_reason="test file path must end with '.py'",
                )
            )
            continue

        normalized_file = file_part.replace("\\", "/")
        if normalized_file == ".py" or normalized_file.endswith(("/.py",)):
            refs.append(
                TestReference(
                    raw=raw_token,
                    file_path_str=file_part,
                    symbols=[],
                    is_malformed=True,
                    malformed_reason="empty test filename",
                )
            )
            continue

        symbols = parts[1:]
        if len(parts) > 1 and (
            not symbols or any(not s.isidentifier() for s in symbols)
        ):
            refs.append(
                TestReference(
                    raw=raw_token,
                    file_path_str=file_part,
                    symbols=symbols,
                    is_malformed=True,
                    malformed_reason="invalid test symbol identifier",
                )
            )
            continue

        refs.append(
            TestReference(
                raw=raw_token,
                file_path_str=file_part,
                symbols=symbols,
            )
        )
    return refs


def parse_guarantee_map(
    doc_content: str, doc_path: Path
) -> tuple[list[GuaranteeRow], list[str]]:
    """Parse Section E of FAILURE_AND_THREAT_MODEL.md into structured GuaranteeRow objects."""
    errors: list[str] = []
    rows: list[GuaranteeRow] = []

    match = SECTION_E_PATTERN.search(doc_content)
    if not match:
        msg = f"Section '## E. Guarantee → test map' not found in {doc_path}"
        errors.append(msg)
        emit_error(msg, str(doc_path))
        return rows, errors

    section_start = match.end()
    lines = doc_content[section_start:].splitlines()
    start_line_offset = doc_content[:section_start].count("\n") + 1

    table_lines: list[tuple[int, str]] = []
    in_table = False

    in_html_comment = False
    for idx, raw_line in enumerate(lines):
        line_num = start_line_offset + idx
        stripped = raw_line.strip()

        # Handle multi-line and standalone HTML comments
        if in_html_comment:
            if "-->" in stripped:
                in_html_comment = False
            continue

        if stripped.startswith("<!--"):
            if not stripped.endswith("-->") or stripped == "<!--":
                in_html_comment = True
            continue

        # Stop if we hit the next major section or section divider (whether table has started or not)
        if stripped.startswith(("## ", "# ")) or stripped == "---":
            break

        if stripped.startswith("|"):
            in_table = True
            table_lines.append((line_num, stripped))
        elif in_table:
            # First non-table line after table start indicates table has ended
            break

    if not table_lines:
        msg = f"No markdown table found in Section E of {doc_path}"
        errors.append(msg)
        emit_error(msg, str(doc_path))
        return rows, errors

    # Parse header
    header_line_num, header_line = table_lines[0]
    header_cols = split_markdown_table_row(header_line)

    if len(header_cols) < 2:
        msg = f"Malformed table header at line {header_line_num}: expected at least 2 columns"
        errors.append(msg)
        emit_error(msg, str(doc_path), header_line_num)
        return rows, errors

    # Determine column indexes flexibly
    id_col_idx = -1
    test_col_idx = -1
    where_col_idx = -1

    for i, col in enumerate(header_cols):
        col_lower = col.lower()
        if col_lower in ("id", "guarantee id"):
            id_col_idx = i
            break
    if id_col_idx == -1:
        for i, col in enumerate(header_cols):
            if "guarantee" in col.lower():
                id_col_idx = i
                break
    if id_col_idx == -1:
        id_col_idx = 0

    for i, col in enumerate(header_cols):
        if "test" in col.lower():
            test_col_idx = i
            break
    if test_col_idx == -1:
        test_col_idx = len(header_cols) - 1

    for i, col in enumerate(header_cols):
        if "where" in col.lower() or "doc" in col.lower():
            where_col_idx = i
            break

    if id_col_idx == test_col_idx:
        msg = (
            f"Malformed table header at line {header_line_num}: "
            f"could not distinguish guarantee identifier and test columns"
        )
        errors.append(msg)
        emit_error(msg, str(doc_path), header_line_num)
        return rows, errors

    seen_ids: dict[str, tuple[int, str]] = {}

    # Skip header and separator rows
    for line_num, line in table_lines[1:]:
        cols = split_markdown_table_row(line)
        # Skip separator row e.g. |---|---|---|
        non_empty_cols = [c for c in cols if c]
        if non_empty_cols and all(re.match(r"^:?-+:?$", c) for c in non_empty_cols):
            continue

        if len(cols) <= max(id_col_idx, test_col_idx):
            msg = (
                f"Malformed table row at line {line_num}: "
                f"expected at least {max(id_col_idx, test_col_idx) + 1} columns, found {len(cols)}"
            )
            errors.append(msg)
            emit_error(msg, str(doc_path), line_num)
            continue

        raw_id = cols[id_col_idx]
        # Clean markdown formatting and normalize whitespace
        clean_id = re.sub(r"[*_`]", "", raw_id).strip()
        clean_id = " ".join(clean_id.split())

        # Validate non-empty and correctly formatted
        if not clean_id:
            msg = f"Empty guarantee identifier at line {line_num}"
            errors.append(msg)
            emit_error(msg, str(doc_path), line_num)
            continue

        if not re.search(r"\w", clean_id):
            msg = (
                f"Malformed guarantee identifier '{raw_id}' at line {line_num}: "
                f"must contain alphanumeric characters"
            )
            errors.append(msg)
            emit_error(msg, str(doc_path), line_num)
            continue

        # Check uniqueness case-insensitively with normalized whitespace
        normalized_id = clean_id.lower()
        if normalized_id in seen_ids:
            first_line, original_name = seen_ids[normalized_id]
            if clean_id == original_name:
                msg = (
                    f"Duplicate guarantee identifier '{clean_id}' at line {line_num} "
                    f"(first defined at line {first_line})"
                )
            else:
                msg = (
                    f"Duplicate guarantee identifier '{clean_id}' at line {line_num} "
                    f"(first defined as '{original_name}' at line {first_line})"
                )
            errors.append(msg)
            emit_error(msg, str(doc_path), line_num)
            continue
        seen_ids[normalized_id] = (line_num, clean_id)

        doc_ref = (
            cols[where_col_idx]
            if where_col_idx >= 0 and where_col_idx < len(cols)
            else ""
        )
        tests_raw = cols[test_col_idx]
        test_refs = parse_test_references(tests_raw)

        has_malformed = False
        for ref in test_refs:
            if ref.is_malformed:
                has_malformed = True
                msg = (
                    f"Guarantee '{clean_id}' at line {line_num} has malformed test reference "
                    f"'{ref.raw}': {ref.malformed_reason}"
                )
                errors.append(msg)
                emit_error(msg, str(doc_path), line_num)

        valid_refs = [r for r in test_refs if not r.is_malformed]
        if not valid_refs and not has_malformed:
            msg = f"Guarantee '{clean_id}' at line {line_num} has no valid test references"
            errors.append(msg)
            emit_error(msg, str(doc_path), line_num)
            continue

        rows.append(
            GuaranteeRow(
                identifier=clean_id,
                doc_ref=doc_ref,
                tests_raw=tests_raw,
                test_refs=test_refs,
                line_number=line_num,
            )
        )

    return rows, errors


def resolve_test_file(
    root: Path, file_path_str: str, custom_test_dir: Path | None = None
) -> Path | None:
    """Resolve a test file path against candidate test roots, enforcing exact casing across all components."""
    # Reject external URLs, UNC network paths, and absolute paths
    if file_path_str.startswith(("//", "\\\\", "/", "\\")) or "://" in file_path_str:
        return None

    # Normalize path separators across platforms
    normalized_path_str = file_path_str.replace("\\", "/")
    path_obj = Path(*(normalized_path_str.split("/")))
    if path_obj.is_absolute():
        return None

    candidates: list[tuple[Path, Path]] = []
    seen_candidates: set[Path] = set()

    def add_candidate(base: Path, candidate_path: Path) -> None:
        if candidate_path not in seen_candidates:
            seen_candidates.add(candidate_path)
            candidates.append((base, candidate_path))

    if len(path_obj.parts) == 1:
        # Bare filename e.g. "test_version.py"
        if custom_test_dir:
            add_candidate(custom_test_dir, custom_test_dir / path_obj)
        add_candidate(root, root / "sdk" / "tests" / path_obj)
        add_candidate(root, root / "tests" / path_obj)
        add_candidate(root, root / path_obj)
    elif len(path_obj.parts) > 1:
        # Relative path with directory structure e.g. "tests/test_version.py"
        if custom_test_dir:
            add_candidate(custom_test_dir, custom_test_dir / path_obj)
        add_candidate(root, root / path_obj)
        add_candidate(root, root / "sdk" / path_obj)

    for base, c in candidates:
        try:
            if c.is_file():
                res = c.resolve()
                base_res = base.resolve()
                # Enforce path confinement within base directory
                if not res.is_relative_to(base_res):
                    continue
                # Enforce exact case sensitivity for all path components
                if c.relative_to(base).parts == res.relative_to(base_res).parts:
                    return res
        except (OSError, ValueError):
            continue

    return None


def validate_guarantee_map(
    root: Path,
    doc_path: Path | str | None = None,
    test_dir: Path | str | None = None,
) -> tuple[bool, list[str]]:
    """Validate all guarantee identifiers, referenced test files, and test symbols."""
    errors: list[str] = []
    doc = (root / (Path(doc_path) if doc_path else Path(DEFAULT_DOC))).resolve()
    test_directory = (
        (root / Path(test_dir)).resolve()
        if test_dir
        else (root / DEFAULT_TEST_DIR).resolve()
    )

    if not doc.exists():
        msg = f"Documentation file not found: {doc}"
        emit_error(msg, str(doc))
        return False, [msg]

    try:
        content = doc.read_text(encoding="utf-8")
    except OSError as e:
        msg = f"Failed to read documentation file {doc}: {e}"
        emit_error(msg, str(doc))
        return False, [msg]

    rows, parse_errors = parse_guarantee_map(content, doc)
    errors.extend(parse_errors)

    # Cache AST symbols per resolved test file
    ast_symbol_cache: dict[Path, SymbolCollection] = {}
    ast_error_cache: dict[Path, str] = {}

    for row in rows:
        for ref in row.test_refs:
            if ref.is_malformed:
                continue
            resolved = resolve_test_file(root, ref.file_path_str, test_directory)
            if not resolved:
                msg = (
                    f"Guarantee '{row.identifier}' references non-existent test file: "
                    f"'{ref.file_path_str}'"
                )
                errors.append(msg)
                emit_error(msg, str(doc), row.line_number)
                continue

            ref.resolved_path = resolved

            # Parse AST if not already cached
            if resolved not in ast_symbol_cache and resolved not in ast_error_cache:
                try:
                    tree = ast.parse(
                        resolved.read_text(encoding="utf-8"), filename=str(resolved)
                    )
                    ast_symbol_cache[resolved] = extract_symbols_from_ast(tree)
                except SyntaxError as se:
                    ast_error_cache[resolved] = (
                        f"SyntaxError parsing test file {resolved}: {se}"
                    )
                except (OSError, UnicodeDecodeError) as oe:
                    ast_error_cache[resolved] = (
                        f"Error reading test file {resolved}: {oe}"
                    )

            if resolved in ast_error_cache:
                msg = f"Guarantee '{row.identifier}': {ast_error_cache[resolved]}"
                errors.append(msg)
                emit_error(msg, str(doc), row.line_number)
                continue

            available_symbols = ast_symbol_cache[resolved]

            # Validate referenced symbol(s)
            if ref.symbols:
                if len(ref.symbols) == 1:
                    # Single symbol matches top-level function, class, or cited class method
                    target = ref.symbols[0]
                    if target not in available_symbols:
                        msg = (
                            f"Guarantee '{row.identifier}' references non-existent test symbol "
                            f"'{target}' in '{ref.file_path_str}'"
                        )
                        errors.append(msg)
                        emit_error(msg, str(doc), row.line_number)
                else:
                    # Multi-part symbol e.g. Class::method must match qualified hierarchy
                    qualified_target = "::".join(ref.symbols)
                    if qualified_target not in available_symbols.qualified:
                        msg = (
                            f"Guarantee '{row.identifier}' references non-existent test symbol "
                            f"'{qualified_target}' in '{ref.file_path_str}'"
                        )
                        errors.append(msg)
                        emit_error(msg, str(doc), row.line_number)

    return len(errors) == 0, errors


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate guarantee-to-test map in FAILURE_AND_THREAT_MODEL.md"
    )
    parser.add_argument("--root", default=".", help="Repository root directory")
    parser.add_argument(
        "--doc",
        default=DEFAULT_DOC,
        help="Path to failure and threat model markdown",
    )
    parser.add_argument(
        "--test-dir",
        default=DEFAULT_TEST_DIR,
        help="Path to tests directory",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    doc_path = Path(args.doc)
    test_dir = Path(args.test_dir)

    valid, errors = validate_guarantee_map(
        root=root,
        doc_path=doc_path,
        test_dir=test_dir,
    )

    if not valid:
        print(
            f"\nGuarantee map validation failed with {len(errors)} error(s).",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Guarantee map verified successfully: all test references and symbols exist.")
    sys.exit(0)


if __name__ == "__main__":
    main()
