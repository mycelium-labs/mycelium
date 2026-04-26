#!/usr/bin/env python3
"""Turn raw classifier predictions into a human-reviewable bulk pack.

After running `classify_corpus.py full`, you have predictions on ~15.8k issues.
Most are "n". This script:

1. Filters to predictions with label != "n" AND confidence in {high, medium}.
2. Joins them with the original issue title/body/url from the HF cache.
3. De-duplicates against incidents/tagged/v0/tagged.jsonl (no need to re-tag what
   you already hand-labeled).
4. Writes:
       incidents/tagged/v1/proposed.jsonl   structured, ready for ingest_proposed.py
       incidents/tagged/v1/proposed.md      human-readable review file

Workflow:
    python scripts/classify_corpus.py full
    python scripts/build_review_pack.py
    # review proposed.md, edit proposed.jsonl if you want to override anything
    python scripts/ingest_proposed.py --version v1
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_DIR = REPO_ROOT / "incidents" / "tagged"
V0_DIR = TAXONOMY_DIR / "v0"
V1_DIR = TAXONOMY_DIR / "v1"
PREDICTIONS = V1_DIR / "predictions.jsonl"
PROPOSED_JSONL = V1_DIR / "proposed.jsonl"
PROPOSED_MD = V1_DIR / "proposed.md"


def already_tagged_ids() -> set[str]:
    p = V0_DIR / "tagged.jsonl"
    if not p.exists():
        return set()
    out = set()
    for line in p.read_text().splitlines():
        if line.strip():
            try:
                out.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                pass
    return out


def normalize_label(raw: str) -> list[str]:
    parts = [p.strip() for p in (raw or "").split("+") if p.strip()]
    return [p for p in parts if p.startswith("AF-")]


def main() -> int:
    if not PREDICTIONS.exists():
        print(f"ERROR: {PREDICTIONS} not found.", file=sys.stderr)
        print("Run: python scripts/classify_corpus.py full", file=sys.stderr)
        return 2

    preds = [json.loads(line) for line in PREDICTIONS.read_text().splitlines() if line.strip()]
    skip_ids = already_tagged_ids()

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
        if p["id"] in skip_ids:
            continue
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
        f"LLM-proposed AF-* tags from {len(preds):,} classified issues.",
        f"Filtered to confidence ∈ {{high, medium}}, deduped against v0 hand-tags.",
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
        f"| Already in v0 (excluded from review) | {sum(1 for p in preds if p['id'] in skip_ids):,} |",
        f"| **Proposals to review** | **{len(af_preds):,}** |",
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

    md_lines += [
        "",
        "## AF-tag distribution by repo (high+medium only, in review pack)",
        "",
        "| Repo | " + " | ".join(sorted({lbl for c in repo_label_dist.values() for lbl in c})) + " |",
        "|---|" + "|".join(["--:"] * len({lbl for c in repo_label_dist.values() for lbl in c})) + "|",
    ]
    all_labels = sorted({lbl for c in repo_label_dist.values() for lbl in c})
    for repo in sorted(repo_label_dist):
        cells = [str(repo_label_dist[repo].get(lbl, 0)) for lbl in all_labels]
        md_lines.append(f"| {repo} | " + " | ".join(cells) + " |")

    md_lines += [
        "",
        "## How to review",
        "",
        "1. Skim each AF section below.",
        "2. To override a verdict, edit `incidents/tagged/v1/proposed.jsonl`",
        "   directly (or tell Claude which `idx` to flip).",
        "3. Ingest:  `python scripts/ingest_proposed.py --version v1`",
        "",
    ]

    for lbl in sorted(by_label):
        items = sorted(by_label[lbl], key=lambda p: (0 if p["confidence"] == "high" else 1, p["repo"], p["number"]))
        md_lines.append("---")
        md_lines.append("")
        md_lines.append(f"## {lbl} — {len(items)} candidates")
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
