#!/usr/bin/env python3
"""Aggregate AF-* frequencies from Hugging Face predictions/*.jsonl.

Reads the same dataset as classify_corpus.py (MYCELIUM_HF_REPO / ndileep/mycelium-agent-failures).
Each row may contribute to multiple AF-* counts if label is compound (e.g. AF-006+AF-009).

Usage:
    python scripts/analyze_af_frequency.py
    python scripts/analyze_af_frequency.py --markdown   # print GitHub-flavored table
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

REPOS = [
    "langchain-ai/langchain",
    "langchain-ai/langgraph",
    "microsoft/autogen",
    "crewAIInc/crewAI",
    "openai/openai-agents-python",
    "huggingface/smolagents",
    "All-Hands-AI/OpenHands",
    "cline/cline",
    "browserbase/stagehand",
    "livekit/agents",
]

DEFAULT_HF_REPO = "ndileep/mycelium-agent-failures"
LOCAL_PRED_CACHE = REPO_ROOT / ".cache" / "predictions"
AF_PATTERN = re.compile(r"^AF-(\d{3})$", re.IGNORECASE)


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass


def hf_repo_id() -> str:
    return (os.environ.get("MYCELIUM_HF_REPO") or DEFAULT_HF_REPO).strip()


def hf_predictions_path(repo: str) -> str:
    _, name = repo.split("/", 1)
    return f"predictions/{name}.jsonl"


def parse_label(label: str | None) -> tuple[list[str], bool]:
    """Return (list of AF-XXX ids), is_negative."""
    if not label or not str(label).strip():
        return [], True
    s = str(label).strip()
    if s.lower() in ("n", "none", "not-a-failure"):
        return [], True
    tags: list[str] = []
    for part in s.split("+"):
        p = part.strip()
        m = AF_PATTERN.match(p)
        if m:
            tags.append(f"AF-{m.group(1)}")
    return tags, len(tags) == 0


def download_predictions_rows(repo_id: str, repo: str) -> list[dict]:
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import EntryNotFoundError

    LOCAL_PRED_CACHE.mkdir(parents=True, exist_ok=True)
    path = hf_predictions_path(repo)
    try:
        local = hf_hub_download(
            repo_id,
            path,
            repo_type="dataset",
            local_dir=str(LOCAL_PRED_CACHE / "_hf"),
        )
    except EntryNotFoundError:
        return []
    rows = []
    with open(local) as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--markdown", action="store_true", help="Print markdown table to stdout")
    args = parser.parse_args()

    load_env()
    repo_id = hf_repo_id()

    try:
        from huggingface_hub import get_token
    except ImportError:
        print("ERROR: huggingface_hub not installed.", file=sys.stderr)
        return 2

    if not (os.environ.get("HF_TOKEN") or get_token()):
        print("ERROR: Set HF_TOKEN or run huggingface-cli login.", file=sys.stderr)
        return 2

    af_counts: Counter[str] = Counter()
    per_repo_af: dict[str, Counter[str]] = {}
    total_rows = 0
    rows_with_af = 0
    rows_negative = 0
    rows_error = 0
    rows_prefilter = 0

    for repo in REPOS:
        per_repo_af[repo] = Counter()
        rows = download_predictions_rows(repo_id, repo)
        for row in rows:
            total_rows += 1
            if row.get("error"):
                rows_error += 1
                continue
            model = (row.get("model") or "")
            if isinstance(model, str) and model.startswith("prefilter:"):
                rows_prefilter += 1
            label = row.get("label")
            tags, is_neg = parse_label(label)
            if not tags:
                rows_negative += 1
            else:
                rows_with_af += 1
                for t in tags:
                    af_counts[t] += 1
                    per_repo_af[repo][t] += 1

    # Sort AF keys numerically
    def af_sort_key(k: str) -> int:
        return int(k.replace("AF-", ""))

    ordered_af = sorted(af_counts.keys(), key=af_sort_key)

    if args.markdown:
        print("### Full corpus (HF `predictions/*.jsonl`)\n")
        print("| Metric | Value |")
        print("|--------|-------|")
        print(f"| Total prediction rows | {total_rows} |")
        print(f"| Rows with ≥1 AF tag | {rows_with_af} |")
        print(f"| Rows tagged `n` / no AF | {rows_negative} |")
        print(f"| Rows with `error` (not counted in labels) | {rows_error} |")
        print(f"| Prefilter rows (subset of negatives) | {rows_prefilter} |")
        print()
        print("| AF-ID | Issue-level occurrences¹ |")
        print("|-------|---------------------------|")
        for aid in ordered_af:
            print(f"| {aid} | {af_counts[aid]} |")
        print()
        print(
            "¹ A single issue with label `AF-006+AF-009` increments both AF-006 and AF-009 by one."
        )
        return 0

    print(f"HF dataset: {repo_id}\n")
    print(f"Total rows: {total_rows}")
    print(f"With ≥1 AF: {rows_with_af}  |  n / no AF: {rows_negative}  |  errors: {rows_error}")
    print(f"Prefilter rows: {rows_prefilter}\n")
    print("AF frequency (each compound label counts each mode):")
    for aid in ordered_af:
        print(f"  {aid}: {af_counts[aid]}")
    print("\nPer-repo AF totals (sum of modes):")
    for repo in REPOS:
        c = per_repo_af[repo]
        if not sum(c.values()):
            continue
        print(f"  {repo}: {dict(sorted(c.items(), key=lambda x: af_sort_key(x[0])))}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
