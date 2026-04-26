#!/usr/bin/env python3
"""Append AI-proposed tags from `proposed.jsonl` into `tagged.jsonl`.

Idempotent: skips any issue id that's already tagged. Run as many times as you
like; only new entries get appended.

Workflow:
    # 1. Review incidents/tagged/v{0,1}/proposed.md (and edit proposed.jsonl
    #    if you want to override any verdicts).
    # 2. Ingest:
    python scripts/ingest_proposed.py            # v0 (default)
    python scripts/ingest_proposed.py --version v1

    # 3. Sanity check:
    python scripts/tag_next.py status
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def paths_for(version: str) -> tuple[Path, Path]:
    base = REPO_ROOT / "incidents" / "tagged" / version
    return base / "proposed.jsonl", base / "tagged.jsonl"


def load_existing_ids(tagged: Path) -> set[str]:
    if not tagged.exists():
        return set()
    ids: set[str] = set()
    for line in tagged.read_text().splitlines():
        if line.strip():
            ids.add(json.loads(line)["id"])
    return ids


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="v0", help="catalog version (subdir under incidents/tagged/, default v0)")
    parser.add_argument("--tagged-by", default="claude-proposed-human-reviewed", help="who/what produced the tags")
    args = parser.parse_args()

    proposed_path, tagged_path = paths_for(args.version)
    if not proposed_path.exists():
        print(f"ERROR: {proposed_path} not found", file=sys.stderr)
        return 2

    existing = load_existing_ids(tagged_path)
    proposed = [json.loads(line) for line in proposed_path.read_text().splitlines() if line.strip()]

    appended = 0
    skipped_existing = 0
    now = datetime.now(timezone.utc).isoformat()
    tagged_path.parent.mkdir(parents=True, exist_ok=True)

    with tagged_path.open("a") as fh:
        for prop in proposed:
            if prop["id"] in existing:
                skipped_existing += 1
                continue

            entry = {
                "id": prop["id"],
                "repo": prop["repo"],
                "number": prop["number"],
                "url": prop["url"],
                "title": prop["title"],
                "tagged_at": now,
                "tagged_by": args.tagged_by,
                "status": prop["status"],
                "labels": prop.get("labels", []),
                "confidence": prop.get("confidence"),
                "evidence": prop.get("evidence", ""),
                "notes": prop.get("notes", ""),
            }
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            appended += 1

    print(f"version:         {args.version}")
    print(f"target file:     {tagged_path.relative_to(REPO_ROOT)}")
    print(f"appended:        {appended}")
    print(f"skipped (exist): {skipped_existing}")
    print(f"total proposed:  {len(proposed)}")
    print(f"\nNext: python scripts/tag_next.py status (v0 only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
