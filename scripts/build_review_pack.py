#!/usr/bin/env python3
"""Export a readable report of high/medium AF-* hits from HF predictions.

The **catalog of record** is the Hugging Face dataset (`predictions/<repo>.jsonl`)
produced by `classify_corpus.py run`. This script only materializes a filtered
view under `incidents/tagged/v1/` for skimming (markdown + jsonl) - not a
second “approved” store and not merged back into anything by default.

    python scripts/classify_corpus.py run   # populate HF
    python scripts/build_review_pack.py     # optional: proposed.md + proposed.jsonl

Auth:
    HF_TOKEN, MYCELIUM_HF_REPO  (same env as scrape / classify scripts)
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_DIR = REPO_ROOT / "incidents" / "tagged"
V1_DIR = TAXONOMY_DIR / "v1"
PROPOSED_JSONL = V1_DIR / "proposed.jsonl"
PROPOSED_MD = V1_DIR / "proposed.md"
LOCAL_PRED_CACHE = REPO_ROOT / ".cache" / "predictions" / "_hf"

DEFAULT_HF_REPO = "ndileep/mycelium-agent-failures"
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


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass


def load_predictions_from_hf() -> list[dict[str, Any]]:
    try:
        from huggingface_hub import HfApi, get_token, hf_hub_download
        from huggingface_hub.utils import EntryNotFoundError
    except ImportError:
        print("ERROR: huggingface_hub not installed", file=sys.stderr)
        sys.exit(2)

    token = os.environ.get("HF_TOKEN") or get_token()
    if not token:
        print("ERROR: no HF token. Set HF_TOKEN or run `huggingface-cli login`.", file=sys.stderr)
        sys.exit(2)
    repo_id = (os.environ.get("MYCELIUM_HF_REPO") or DEFAULT_HF_REPO).strip()
    api = HfApi(token=token)
    LOCAL_PRED_CACHE.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    for repo in REPOS:
        _, name = repo.split("/", 1)
        try:
            local = hf_hub_download(
                repo_id,
                f"predictions/{name}.jsonl",
                repo_type="dataset",
                local_dir=str(LOCAL_PRED_CACHE),
            )
        except EntryNotFoundError:
            print(f"  [{name}] no predictions yet, skipping", file=sys.stderr)
            continue
        with open(local) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        print(f"  [{name}] loaded {sum(1 for r in rows if r.get('repo') == repo)} rows", file=sys.stderr)
    return rows


def normalize_label(raw: str) -> list[str]:
    parts = [p.strip() for p in (raw or "").split("+") if p.strip()]
    return [p for p in parts if p.startswith("AF-")]


def main() -> int:
    load_env()
    print(f"[hf] pulling predictions from {os.environ.get('MYCELIUM_HF_REPO') or DEFAULT_HF_REPO}", file=sys.stderr)
    preds = load_predictions_from_hf()
    if not preds:
        print("ERROR: no predictions found on HF. Run `python scripts/classify_corpus.py run` first.", file=sys.stderr)
        return 2

    af_preds: list[dict[str, Any]] = []
    label_dist: Counter[str] = Counter()
    confidence_dist: Counter[str] = Counter()
    repo_label_dist: dict[str, Counter] = defaultdict(Counter)

    for p in preds:
        if "error" in p:
            continue
        labels = normalize_label(p.get("label", ""))
        confidence_dist[p.get("confidence", "?")] += 1
        if not labels:
            label_dist["n"] += 1
            continue
        for lbl in labels:
            label_dist[lbl] += 1
            repo_label_dist[p["repo"]][lbl] += 1
        if p.get("confidence") not in ("high", "medium"):
            continue
        af_preds.append(p)

    af_preds.sort(key=lambda p: (
        0 if p.get("confidence") == "high" else 1,
        p["repo"],
        p["number"],
    ))

    V1_DIR.mkdir(parents=True, exist_ok=True)
    with PROPOSED_JSONL.open("w") as fh:
        for i, p in enumerate(af_preds, 1):
            entry = {
                "idx": i,
                "id": p["id"],
                "repo": p["repo"],
                "number": p["number"],
                "url": p["url"],
                "title": p["title"],
                "status": "tagged",
                "labels": normalize_label(p["label"]),
                "confidence": p["confidence"],
                "evidence": p.get("evidence", ""),
                "notes": "llm-proposed; " + (p.get("reasoning") or ""),
            }
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    by_label: dict[str, list[dict]] = defaultdict(list)
    for p in af_preds:
        for lbl in normalize_label(p["label"]):
            by_label[lbl].append(p)

    high_count = sum(1 for p in af_preds if p["confidence"] == "high")
    med_count = sum(1 for p in af_preds if p["confidence"] == "medium")

    md_lines = [
        "# v1 Bulk-Classified Review Pack",
        "",
        f"LLM-proposed AF-* tags from {len(preds):,} classified issues (HF predictions/).",
        f"Filtered to confidence ∈ {{high, medium}} (HF is the source of truth).",
        "",
        "## Summary",
        "",
        "| Metric | Count |",
        "|---|---|",
        f"| Total issues classified | {len(preds):,} |",
        f"| Errors / skipped | {sum(1 for p in preds if 'error' in p):,} |",
        f"| Predicted not-a-failure | {label_dist.get('n', 0):,} |",
        f"| Predicted AF-* (any confidence) | {sum(v for k, v in label_dist.items() if k != 'n'):,} |",
        f"| AF-* high-confidence | {high_count:,} |",
        f"| AF-* medium-confidence | {med_count:,} |",
        f"| **AF proposals in this export** | **{len(af_preds):,}** |",
        "",
        "## Confidence distribution (full corpus)",
        "",
        "| Confidence | Count |",
        "|---|--:|",
    ]
    for k, v in sorted(confidence_dist.items(), key=lambda kv: (-kv[1], kv[0])):
        md_lines.append(f"| {k} | {v:,} |")

    md_lines += [
        "",
        "## AF-tag distribution (full corpus, all confidences)",
        "",
        "| Label | Count |",
        "|---|--:|",
    ]
    for k, v in sorted(label_dist.items(), key=lambda kv: (-kv[1], kv[0])):
        if k == "n":
            continue
        md_lines.append(f"| {k} | {v:,} |")

    if repo_label_dist:
        all_labels = sorted({lbl for c in repo_label_dist.values() for lbl in c})
        md_lines += [
            "",
            "## AF-tag distribution by repo (high+medium only, in review pack)",
            "",
            "| Repo | " + " | ".join(all_labels) + " |",
            "|---|" + "|".join(["--:"] * len(all_labels)) + "|",
        ]
        for repo in sorted(repo_label_dist):
            cells = [str(repo_label_dist[repo].get(lbl, 0)) for lbl in all_labels]
            md_lines.append(f"| {repo} | " + " | ".join(cells) + " |")

    md_lines += [
        "",
        "## Notes",
        "",
        "Authoritative labels live on Hugging Face (`predictions/<repo>.jsonl`).",
        "This folder is a convenience export for reading and sharing - not a second database.",
        "",
    ]

    for lbl in sorted(by_label):
        items = sorted(by_label[lbl], key=lambda p: (0 if p["confidence"] == "high" else 1, p["repo"], p["number"]))
        md_lines.append("---")
        md_lines.append("")
        md_lines.append(f"## {lbl} - {len(items)} candidates")
        md_lines.append("")
        for p in items:
            md_lines.append(f"### `{p['id']}` ({p['confidence']})")
            md_lines.append(f"> {p['title']}")
            md_lines.append("")
            md_lines.append(f"**URL:** {p['url']}")
            md_lines.append("")
            ev = (p.get('evidence') or '').replace('\n', ' ')
            rs = (p.get('reasoning') or '').replace('\n', ' ')
            md_lines.append(f"**evidence:** {ev}")
            md_lines.append("")
            md_lines.append(f"**reasoning:** {rs}")
            md_lines.append("")

    PROPOSED_MD.write_text("\n".join(md_lines))

    print(f"wrote {PROPOSED_JSONL.relative_to(REPO_ROOT)} ({len(af_preds)} proposals)")
    print(f"wrote {PROPOSED_MD.relative_to(REPO_ROOT)}")
    print()
    print("label distribution (full corpus):")
    for k, v in sorted(label_dist.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {k:8s} {v:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
