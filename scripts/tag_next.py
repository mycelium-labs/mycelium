#!/usr/bin/env python3
"""LEGACY interactive tagging - builds `incidents/tagged/v0/` for regression tests.

The live failure-mode catalog is **only** Hugging Face `predictions/<repo>.jsonl`
from `python scripts/classify_corpus.py run` (prefilter + Claude).

Keep using this script **only** if you need to extend the frozen v0 pair used by
`classify_corpus.py validate` / `validate-prefilter`. It is not the product
database.

Workflow (optional, for v0 fixture maintenance):
    python scripts/tag_next.py build-queue
    python scripts/tag_next.py
    python scripts/tag_next.py status

Outputs:
    incidents/tagged/v0/queue.jsonl  … sample for pairing with tagged.jsonl
    incidents/tagged/v0/tagged.jsonl … labels used by validate commands only

Auth (build-queue only): HF_TOKEN, MYCELIUM_HF_REPO
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_DIR = REPO_ROOT / "incidents" / "tagged"
TAGGED_DIR = TAXONOMY_DIR / "v0"
QUEUE_FILE = TAGGED_DIR / "queue.jsonl"
TAGGED_FILE = TAGGED_DIR / "tagged.jsonl"

DEFAULT_HF_REPO = "ndileep/mycelium-agent-failures"
SAMPLE_PER_REPO = 5
RANDOM_SEED = 42  # reproducible queue

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


# -----------------------------------------------------------------------------
# Taxonomy (read from disk so SKILL.md files stay the single source of truth)
# -----------------------------------------------------------------------------


def load_taxonomy() -> dict[str, dict[str, str]]:
    """Return {AF-001: {name, oneline, signal, fix}}."""
    out: dict[str, dict[str, str]] = {}
    for path in sorted(TAXONOMY_DIR.glob("AF-*.md")):
        text = path.read_text()
        af_id = path.name.split("-", 1)[0] + "-" + path.name.split("-")[1]
        m_name = re.match(r"#\s*(AF-\d+)\s*[--]\s*(.+)", text.splitlines()[0])
        name = m_name.group(2).strip() if m_name else af_id

        def field(label: str) -> str:
            m = re.search(rf"\*\*{re.escape(label)}:\*\*\s*(.+)", text)
            return m.group(1).strip() if m else ""

        out[af_id] = {
            "name": name,
            "oneline": field("One-line"),
            "signal": field("Detection signal"),
            "fix": field("Runtime fix"),
        }
    return out


def print_taxonomy(tax: dict[str, dict[str, str]]) -> None:
    print()
    print("Taxonomy (pick by number):")
    print("-" * 78)
    for af_id, meta in tax.items():
        n = af_id.split("-")[1].lstrip("0") or "0"
        print(f"  {n}. {af_id}  {meta['name']}")
        print(f"     {meta['oneline']}")
    print("-" * 78)


# -----------------------------------------------------------------------------
# build-queue: stratified random sample from HF
# -----------------------------------------------------------------------------


def _hf_repo_id() -> str:
    return (os.environ.get("MYCELIUM_HF_REPO") or DEFAULT_HF_REPO).strip()


def build_queue() -> int:
    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError:
        print("ERROR: huggingface_hub not installed.\n  uv pip install huggingface_hub", file=sys.stderr)
        return 2

    if QUEUE_FILE.exists():
        print(f"queue already exists at {QUEUE_FILE}")
        print("delete it manually if you want to rebuild (this resets your tagging order).")
        return 1

    repo_id = _hf_repo_id()
    print(f"[hf] reading {repo_id}", file=sys.stderr)
    api = HfApi()
    rng = random.Random(RANDOM_SEED)

    cache = REPO_ROOT / ".cache" / "hf-dl"
    cache.mkdir(parents=True, exist_ok=True)

    sampled: list[dict[str, Any]] = []
    for repo in REPOS:
        _, name = repo.split("/", 1)
        # Pick the most recent full-*.jsonl for each repo.
        tree = list(api.list_repo_tree(repo_id, path_in_repo=f"github-issues/{name}", repo_type="dataset"))
        full_files = sorted([f for f in tree if "full-" in f.path and f.path.endswith(".jsonl")])
        if not full_files:
            print(f"  [{repo}] no full-*.jsonl found, skipping", file=sys.stderr)
            continue
        target = full_files[-1]
        print(f"  [{repo}] downloading {target.path}", file=sys.stderr)
        local = hf_hub_download(repo_id, target.path, repo_type="dataset", local_dir=str(cache))

        with open(local) as fh:
            issues = [json.loads(line) for line in fh if line.strip()]

        if not issues:
            continue

        # Sample N (or all, if fewer). Skip pull requests (defensive - already filtered).
        issues = [i for i in issues if "pull_request" not in i]
        n = min(SAMPLE_PER_REPO, len(issues))
        picks = rng.sample(issues, n)
        for issue in picks:
            sampled.append({
                "id": f"{repo}#{issue['number']}",
                "repo": repo,
                "number": issue["number"],
                "url": issue["html_url"],
                "title": issue["title"],
                "body": issue.get("body") or "",
                "state": issue["state"],
                "labels": [lbl["name"] for lbl in issue.get("labels", [])],
                "created_at": issue["created_at"],
                "updated_at": issue["updated_at"],
                "comments": issue.get("comments", 0),
            })
        print(f"  [{repo}] sampled {n}/{len(issues)}", file=sys.stderr)

    rng.shuffle(sampled)  # interleave repos so you don't tag 5 langchain in a row

    TAGGED_DIR.mkdir(parents=True, exist_ok=True)
    with QUEUE_FILE.open("w") as fh:
        for entry in sampled:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    print(f"\n✓ wrote {len(sampled)} issues to {QUEUE_FILE.relative_to(REPO_ROOT)}", file=sys.stderr)
    print("\nNext: `python scripts/tag_next.py` to start tagging.", file=sys.stderr)
    return 0


# -----------------------------------------------------------------------------
# tag: interactive prompt
# -----------------------------------------------------------------------------


def load_queue() -> list[dict[str, Any]]:
    if not QUEUE_FILE.exists():
        print(f"ERROR: no queue at {QUEUE_FILE}", file=sys.stderr)
        print("Run: python scripts/tag_next.py build-queue", file=sys.stderr)
        sys.exit(2)
    return [json.loads(line) for line in QUEUE_FILE.read_text().splitlines() if line.strip()]


def load_tagged_ids() -> set[str]:
    if not TAGGED_FILE.exists():
        return set()
    ids = set()
    for line in TAGGED_FILE.read_text().splitlines():
        if line.strip():
            ids.add(json.loads(line)["id"])
    return ids


def append_tagged(entry: dict[str, Any]) -> None:
    TAGGED_DIR.mkdir(parents=True, exist_ok=True)
    with TAGGED_FILE.open("a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def prompt(msg: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    val = input(f"{msg}{suffix}: ").strip()
    return val or default


def parse_labels(raw: str, tax: dict[str, dict[str, str]]) -> list[str]:
    af_ids = list(tax.keys())
    out: list[str] = []
    for tok in raw.replace(" ", "").split(","):
        if not tok:
            continue
        try:
            idx = int(tok) - 1
            if 0 <= idx < len(af_ids):
                out.append(af_ids[idx])
            else:
                raise ValueError
        except ValueError:
            print(f"  ! ignored '{tok}' (must be 1..{len(af_ids)})", file=sys.stderr)
    return out


def open_in_browser(url: str) -> None:
    cmd = "open" if sys.platform == "darwin" else "xdg-open"
    try:
        subprocess.run([cmd, url], check=False)
    except FileNotFoundError:
        print(f"  (couldn't open browser; visit: {url})", file=sys.stderr)


def render_issue(issue: dict[str, Any], idx: int, total: int, remaining: int) -> None:
    body = (issue["body"] or "").strip()
    body_preview = "\n".join(body.splitlines()[:40])
    if len(body.splitlines()) > 40:
        body_preview += f"\n  … ({len(body.splitlines()) - 40} more lines, hit 'o' to open in browser)"

    print()
    print("=" * 78)
    print(f"#{idx + 1} of {total}  ({remaining} un-tagged remaining)")
    print(f"repo:    {issue['repo']}")
    print(f"issue:   #{issue['number']}  ({issue['state']})")
    print(f"url:     {issue['url']}")
    if issue.get("labels"):
        print(f"gh tags: {', '.join(issue['labels'][:6])}")
    print(f"updated: {issue['updated_at']}")
    print("=" * 78)
    print(f"TITLE: {issue['title']}")
    print("-" * 78)
    print(textwrap.indent(body_preview or "(no body)", "  "))
    print("=" * 78)


def tag_one(issue: dict[str, Any], idx: int, total: int, tax: dict[str, dict[str, str]], tagged_ids: set[str]) -> str:
    """Returns 'continue', 'quit'."""
    while True:
        render_issue(issue, idx, total, total - len(tagged_ids))
        print_taxonomy(tax)
        print("Commands: 1-9 = AF tag (comma-sep for multi),  s=skip,  n=not-failure,  o=open in browser,  q=save+quit")
        raw = prompt("tag")

        if not raw:
            continue
        if raw == "q":
            return "quit"
        if raw == "o":
            open_in_browser(issue["url"])
            continue
        if raw == "s":
            entry = {
                "id": issue["id"], "repo": issue["repo"], "number": issue["number"], "url": issue["url"],
                "title": issue["title"], "tagged_at": datetime.now(timezone.utc).isoformat(),
                "tagged_by": os.environ.get("USER") or "unknown",
                "status": "skip", "labels": [], "confidence": None, "evidence": "", "notes": "",
            }
            append_tagged(entry)
            tagged_ids.add(issue["id"])
            print(f"  → skipped\n")
            return "continue"
        if raw == "n":
            note = prompt("why not a failure? (e.g. 'feature request', 'docs bug', 'install issue')")
            entry = {
                "id": issue["id"], "repo": issue["repo"], "number": issue["number"], "url": issue["url"],
                "title": issue["title"], "tagged_at": datetime.now(timezone.utc).isoformat(),
                "tagged_by": os.environ.get("USER") or "unknown",
                "status": "not-a-failure", "labels": [], "confidence": None, "evidence": "", "notes": note,
            }
            append_tagged(entry)
            tagged_ids.add(issue["id"])
            print(f"  → marked not-a-failure\n")
            return "continue"

        labels = parse_labels(raw, tax)
        if not labels:
            print("  ! no valid AF tag parsed; try again (e.g. '3' or '3,6')")
            continue

        confidence = prompt("confidence (h/m/l)", "m").lower()
        if confidence not in ("h", "m", "l"):
            confidence = "m"

        evidence = prompt("evidence (1-line quote or signal - required)")
        while not evidence:
            print("  ! evidence is required (forces you to think; this is the data we'll learn from)")
            evidence = prompt("evidence (1-line quote or signal)")

        notes = prompt("notes (optional, enter to skip)")

        entry = {
            "id": issue["id"], "repo": issue["repo"], "number": issue["number"], "url": issue["url"],
            "title": issue["title"], "tagged_at": datetime.now(timezone.utc).isoformat(),
            "tagged_by": os.environ.get("USER") or "unknown",
            "status": "tagged", "labels": labels, "confidence": {"h": "high", "m": "medium", "l": "low"}[confidence],
            "evidence": evidence, "notes": notes,
        }
        append_tagged(entry)
        tagged_ids.add(issue["id"])
        print(f"  ✓ tagged {', '.join(labels)} (confidence={entry['confidence']})\n")
        return "continue"


def cmd_tag() -> int:
    queue = load_queue()
    tagged_ids = load_tagged_ids()
    tax = load_taxonomy()

    if not tax:
        print("ERROR: no taxonomy files found in incidents/tagged/AF-*.md", file=sys.stderr)
        return 2

    todo = [(i, issue) for i, issue in enumerate(queue) if issue["id"] not in tagged_ids]
    if not todo:
        print(f"All {len(queue)} issues tagged. Run `tag_next.py status` to see breakdown.")
        return 0

    print(f"Resuming: {len(tagged_ids)} done, {len(todo)} to go.")
    print("(Ctrl-C is safe - each tag is appended immediately, so you can quit anytime.)")

    for idx, issue in todo:
        try:
            action = tag_one(issue, idx, len(queue), tax, tagged_ids)
        except KeyboardInterrupt:
            print("\n  (interrupted; progress saved)")
            return 0
        if action == "quit":
            print("\n  (saved; resume later with `tag_next.py`)")
            return 0

    print(f"\n✓ done - all {len(queue)} issues tagged. See `tag_next.py status`.")
    return 0


# -----------------------------------------------------------------------------
# status
# -----------------------------------------------------------------------------


def cmd_status() -> int:
    queue = load_queue() if QUEUE_FILE.exists() else []
    tagged: list[dict[str, Any]] = []
    if TAGGED_FILE.exists():
        tagged = [json.loads(line) for line in TAGGED_FILE.read_text().splitlines() if line.strip()]

    print(f"queue:  {len(queue)} issues")
    print(f"tagged: {len(tagged)} entries")
    print()

    if not tagged:
        return 0

    by_status: dict[str, int] = {}
    by_label: dict[str, int] = {}
    by_repo: dict[str, dict[str, int]] = {}
    for t in tagged:
        by_status[t["status"]] = by_status.get(t["status"], 0) + 1
        for lbl in t.get("labels", []):
            by_label[lbl] = by_label.get(lbl, 0) + 1
        by_repo.setdefault(t["repo"], {}).setdefault(t["status"], 0)
        by_repo[t["repo"]][t["status"]] = by_repo[t["repo"]].get(t["status"], 0) + 1

    print("by status:")
    for k, v in sorted(by_status.items()):
        print(f"  {k:18s} {v}")
    print()
    print("by label (multi-tagged issues counted once per label):")
    for k, v in sorted(by_label.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {k}  {v}")
    print()
    print("by repo:")
    for repo in sorted(by_repo):
        parts = [f"{k}={v}" for k, v in sorted(by_repo[repo].items())]
        print(f"  {repo:35s} {' '.join(parts)}")
    return 0


# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Hand-tag issues against the Mycelium failure-mode taxonomy.")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("build-queue", help="Sample 5 issues per repo from HF and write incidents/tagged/v0/queue.jsonl")
    sub.add_parser("status", help="Show tagging progress and label distribution")
    sub.add_parser("tag", help="Tag the next un-tagged issue (default if no subcommand)")
    args = parser.parse_args()

    if args.cmd == "build-queue":
        return build_queue()
    if args.cmd == "status":
        return cmd_status()
    return cmd_tag()


if __name__ == "__main__":
    sys.exit(main())
