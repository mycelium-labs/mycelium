#!/usr/bin/env python3
"""Append AI-proposed tags from `proposed.jsonl` into `tagged.jsonl`.

Idempotent: skips any issue id that's already tagged. Run as many times as you
like; only new entries get appended.

Workflow:
    # 1. Review incidents/tagged/v0/proposed.md (and edit proposed.jsonl
    #    if you want to override any verdicts).
    # 2. Ingest:
    python scripts/ingest_proposed.py

    # 3. Sanity check:
    python scripts/tag_next.py status
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TAGGED_DIR = REPO_ROOT / "incidents" / "tagged" / "v0"
PROPOSED = TAGGED_DIR / "proposed.jsonl"
TAGGED = TAGGED_DIR / "tagged.jsonl"


def load_existing_ids() -> set[str]:
    if not TAGGED.exists():
        return set()
    ids: set[str] = set()
    for line in TAGGED.read_text().splitlines():
        if line.strip():
            ids.add(json.loads(line)["id"])
    return ids


def main() -> int:
    if not PROPOSED.exists():
        print(f"ERROR: {PROPOSED} not found", file=sys.stderr)
        return 2

    existing = load_existing_ids()
    proposed = [json.loads(line) for line in PROPOSED.read_text().splitlines() if line.strip()]

    appended = 0
    skipped_existing = 0
    now = datetime.now(timezone.utc).isoformat()

    with TAGGED.open("a") as fh:
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
                "tagged_by": "claude-proposed-human-reviewed",
                "status": prop["status"],
                "labels": prop.get("labels", []),
                "confidence": prop.get("confidence"),
                "evidence": prop.get("evidence", ""),
                "notes": prop.get("notes", ""),
            }
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            appended += 1

    print(f"appended:        {appended}")
    print(f"skipped (exist): {skipped_existing}")
    print(f"total proposed:  {len(proposed)}")
    print(f"\nNext: python scripts/tag_next.py status")
    return 0


if __name__ == "__main__":
    sys.exit(main())
