#!/usr/bin/env python3
"""Scrape GitHub issues for Mycelium's failure-mode catalog.

Pulls issues (open + closed) from a target repo, buffers them locally as
JSONL, and pushes to a private Hugging Face dataset repo. The Hugging Face
repo is the source of truth; the local buffer exists only so a failed upload
doesn't lose data.

Authentication:
    GITHUB_TOKEN       GitHub personal access token (scope: public_repo)
    HF_TOKEN           Hugging Face access token (scope: write)
    MYCELIUM_HF_REPO   Dataset repo slug, e.g. 'mycelium-labs/agent-failures'

Usage:
    python scripts/scrape_github_issues.py langchain-ai/langchain
    python scripts/scrape_github_issues.py langchain-ai/langchain --since 2026-01-01
    python scripts/scrape_github_issues.py --all
    python scripts/scrape_github_issues.py --all --full         # historical backfill
    python scripts/scrape_github_issues.py langchain-ai/langchain --no-upload

Design notes:
    - Scraping itself is stdlib-only.
    - Upload uses huggingface_hub (pip install huggingface_hub).
    - Pull requests are dropped (GitHub returns PRs under /issues).
    - One commit per scrape run: the day's JSONL + updated manifest in one batch.
    - Full backfills write to `full-YYYY-MM-DD.jsonl` so the next day's
      incremental run cannot clobber them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parent.parent
BUFFER_ROOT = REPO_ROOT / "incidents" / "public" / "github-issues"
REPOS_FILE = REPO_ROOT / "scripts" / "repos.txt"
MANIFEST_FILE = BUFFER_ROOT / "manifest.json"

API = "https://api.github.com"
PAGE_SIZE = 100
USER_AGENT = "mycelium-scraper/0.1"

HF_TOKEN_ENV = "HF_TOKEN"
HF_REPO_ENV = "MYCELIUM_HF_REPO"


# -----------------------------------------------------------------------------
# GitHub scraping (stdlib only)
# -----------------------------------------------------------------------------


def gh_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        print(
            "ERROR: set GITHUB_TOKEN in your environment.\n"
            "  export GITHUB_TOKEN=ghp_...\n"
            "Create one at https://github.com/settings/tokens (scope: public_repo).",
            file=sys.stderr,
        )
        sys.exit(2)
    return tok


def _request(url: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    req = Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {gh_token()}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", USER_AGENT)

    try:
        with urlopen(req) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return body, headers
    except HTTPError as e:
        remaining = e.headers.get("x-ratelimit-remaining") if e.headers else None
        if e.code == 403 and remaining == "0":
            reset = int(e.headers.get("x-ratelimit-reset", "0")) if e.headers else 0
            wait = max(reset - int(time.time()), 10)
            print(f"rate limited, sleeping {wait}s", file=sys.stderr)
            time.sleep(wait)
            return _request(url)
        raise


def fetch_page(
    repo: str,
    page: int,
    since: str | None,
) -> tuple[list[dict[str, Any]], bool]:
    params: dict[str, str | int] = {
        "state": "all",
        "per_page": PAGE_SIZE,
        "page": page,
        "sort": "updated",
        "direction": "desc",
    }
    if since:
        params["since"] = since

    url = f"{API}/repos/{repo}/issues?{urlencode(params)}"
    data, headers = _request(url)
    link = headers.get("link", "")
    issues = [i for i in data if "pull_request" not in i]
    has_next = 'rel="next"' in link
    return issues, has_next


# -----------------------------------------------------------------------------
# Local buffer
# -----------------------------------------------------------------------------


def write_buffer(repo: str, since: str | None, full: bool) -> tuple[Path, int, str]:
    if "/" not in repo:
        raise ValueError(f"repo must be 'owner/name', got: {repo}")

    _, name = repo.split("/", 1)
    out_dir = BUFFER_ROOT / name
    out_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).date().isoformat()
    # Full backfills get their own filename so the next day's incremental
    # cron (which writes `<today>.jsonl`) cannot clobber the historical dump.
    filename = f"full-{today}.jsonl" if full else f"{today}.jsonl"
    out_file = out_dir / filename

    count = 0
    page = 1
    with out_file.open("w", encoding="utf-8") as fh:
        while True:
            print(f"[{repo}] page {page}...", file=sys.stderr, flush=True)
            issues, has_next = fetch_page(repo, page, since)
            if not issues:
                break
            for issue in issues:
                fh.write(json.dumps(issue, ensure_ascii=False) + "\n")
                count += 1
            if not has_next:
                break
            page += 1
            time.sleep(0.25)

    update_manifest(repo, today, count, full)
    print(f"[{repo}] buffered {count} issues → {out_file}", file=sys.stderr)
    return out_file, count, name


def update_manifest(repo: str, date: str, count: int, full: bool) -> None:
    BUFFER_ROOT.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, dict[str, Any]] = {}
    if MANIFEST_FILE.exists():
        manifest = json.loads(MANIFEST_FILE.read_text())
    entry = manifest.get(repo, {})
    if full:
        entry["last_full_scrape"] = date
        entry["last_full_count"] = count
    else:
        entry["last_scraped"] = date
        entry["count"] = count
    manifest[repo] = entry
    MANIFEST_FILE.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")


# -----------------------------------------------------------------------------
# Hugging Face upload (lazy imports; only triggers if configured)
# -----------------------------------------------------------------------------


def hf_configured() -> bool:
    return bool(_hf_token() and _hf_repo())


def _hf_token() -> str:
    return (os.environ.get(HF_TOKEN_ENV) or "").strip()


def _hf_repo() -> str:
    return (os.environ.get(HF_REPO_ENV) or "").strip()


def _validate_hf_repo_id(repo_id: str) -> None:
    if "/" not in repo_id or repo_id.count("/") != 1:
        raise ValueError(
            f"{HF_REPO_ENV} must be in form 'owner/name' (got {repo_id!r}). "
            "Check the GitHub secret for typos or whitespace."
        )
    owner, name = repo_id.split("/")
    if not owner or not name:
        raise ValueError(
            f"{HF_REPO_ENV} must be in form 'owner/name' (got {repo_id!r})."
        )
    if any(ch in repo_id for ch in "\n\r\t "):
        raise ValueError(
            f"{HF_REPO_ENV} contains whitespace or newlines (got {repo_id!r}). "
            "Re-add the GitHub secret without trailing whitespace."
        )


def _require_hf() -> None:
    try:
        import huggingface_hub  # noqa: F401
    except ImportError:
        print(
            "ERROR: huggingface_hub not installed.\n"
            "  uv pip install huggingface_hub  (inside an active .venv)",
            file=sys.stderr,
        )
        sys.exit(2)


_DATASET_CARD = """---
license: other
language:
  - en
pretty_name: Mycelium — Agent Failure Corpus
size_categories:
  - 1K<n<100K
tags:
  - agents
  - ai-safety
  - failure-modes
  - mycelium
---

# Mycelium — Agent Failure Corpus

Private Hugging Face dataset for **Mycelium Labs internal research only**
(building and maintaining the AF-* agent failure-mode taxonomy). **Do not
redistribute** this snapshot or bulk exports without explicit permission.

## Intended use

Taxonomy work, classifier development, and qualitative review — not a
general-purpose open corpus release. For collaboration, contact Mycelium Labs.

## Public sources ingested (GitHub)

Raw issue payloads come from the public GitHub REST API
(`GET /repos/{owner}/{repo}/issues`, `state=all`). Pull requests are excluded
at ingest. The **10** upstream repositories tracked (see `scripts/repos.txt`
in the [Mycelium](https://github.com/mycelium-labs/mycelium) repo) are:

| Upstream repo | Folder under `github-issues/` |
|---------------|--------------------------------|
| `langchain-ai/langchain` | `langchain` |
| `langchain-ai/langgraph` | `langgraph` |
| `microsoft/autogen` | `autogen` |
| `crewAIInc/crewAI` | `crewAI` |
| `openai/openai-agents-python` | `openai-agents-python` |
| `huggingface/smolagents` | `smolagents` |
| `All-Hands-AI/OpenHands` | `OpenHands` |
| `cline/cline` | `cline` |
| `browserbase/stagehand` | `stagehand` |
| `livekit/agents` | `agents` |

Additional public sources (incident DBs, benchmarks, press) may be documented
here when added to the pipeline.

## Licensing stance

- **GitHub content:** Issues are public on GitHub under each repository’s
  license and [GitHub’s terms](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service).
  This dataset is a **snapshot for research**; it does not grant rights beyond
  what applies to the underlying GitHub data.
- **Compilation:** Layout, `predictions/` labels, and manifests are **Mycelium
  Labs research artifacts**. No license to republish the compiled dataset is
  implied.

## Structure

```
github-issues/{repo-name}/YYYY-MM-DD.jsonl       # daily incremental (since yesterday)
github-issues/{repo-name}/full-YYYY-MM-DD.jsonl  # full historical backfill
manifest.json                                    # per-repo scrape state

predictions/{repo-name}.jsonl                    # AF-* classifications, append-only
predictions/manifest.json                        # per-repo classifier state
```

Each line in `github-issues/.../*.jsonl` is the raw GitHub API response for a
single issue. No transformation at ingest time.

Each line in `predictions/{repo-name}.jsonl` is one classification:

    {id, repo, number, url, title,
     label,         # "n" | "AF-XXX" | "AF-XXX+AF-YYY"
     confidence,    # high | medium | low
     evidence, reasoning,
     model, classified_at}

The classifier (`scripts/classify_corpus.py`) is idempotent and append-only
keyed by issue id, so scheduled runs only spend tokens on new issue IDs.

## Update cadence

| Stage | When | Where |
|-------|------|--------|
| **Scrape** | Daily **07:00 UTC** (`cron: 0 7 * * *`) | Workflow `scrape-issues.yml`; manual `workflow_dispatch` or local `scrape_github_issues.py` |
| **Classify** | Every **4 hours** (`cron: 0 */4 * * *`), Groq by default | Workflow `classify-issues.yml` after scrape or on schedule |

Scripts and repo list live in **github.com/mycelium-labs/mycelium**.
"""


def ensure_hf_repo(repo_id: str) -> None:
    _require_hf()
    from huggingface_hub import HfApi

    _validate_hf_repo_id(repo_id)
    api = HfApi(token=_hf_token())
    api.create_repo(
        repo_id=repo_id,
        repo_type="dataset",
        private=True,
        exist_ok=True,
    )

    # Seed the dataset card on first creation. Harmless to re-upload.
    from huggingface_hub.utils import HfHubHTTPError

    try:
        api.upload_file(
            path_or_fileobj=_DATASET_CARD.encode("utf-8"),
            path_in_repo="README.md",
            repo_id=repo_id,
            repo_type="dataset",
            commit_message="scraper: seed dataset card",
        )
    except HfHubHTTPError as e:
        print(f"[hf] dataset card step skipped: {e}", file=sys.stderr)


def push_to_hf(
    repo_id: str,
    local_jsonl: Path,
    repo_name: str,
    today: str,
    full: bool,
) -> None:
    _require_hf()
    from huggingface_hub import CommitOperationAdd, HfApi

    api = HfApi(token=_hf_token())
    filename = f"full-{today}.jsonl" if full else f"{today}.jsonl"
    operations = [
        CommitOperationAdd(
            path_in_repo=f"github-issues/{repo_name}/{filename}",
            path_or_fileobj=str(local_jsonl),
        ),
        CommitOperationAdd(
            path_in_repo="manifest.json",
            path_or_fileobj=str(MANIFEST_FILE),
        ),
    ]
    tag = "full" if full else "incremental"
    api.create_commit(
        repo_id=repo_id,
        repo_type="dataset",
        operations=operations,
        commit_message=f"scraper: {repo_name} {today} ({tag})",
    )
    print(f"[hf] pushed {repo_name}/{filename} → {repo_id}", file=sys.stderr)


# -----------------------------------------------------------------------------
# Orchestration
# -----------------------------------------------------------------------------


def scrape(repo: str, since: str | None, upload: bool, full: bool) -> int:
    out_file, count, name = write_buffer(repo, since, full)

    if upload:
        if not hf_configured():
            print(
                "WARNING: upload requested but HF is not configured.\n"
                f"  Set {HF_TOKEN_ENV} and {HF_REPO_ENV}, or re-run with --no-upload.",
                file=sys.stderr,
            )
            return count
        repo_id = _hf_repo()
        today = datetime.now(timezone.utc).date().isoformat()
        ensure_hf_repo(repo_id)
        push_to_hf(repo_id, out_file, name, today, full)

    return count


def load_repos_file() -> list[str]:
    if not REPOS_FILE.exists():
        return []
    return [
        line.strip()
        for line in REPOS_FILE.read_text().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scrape GitHub issues into a Hugging Face dataset repo."
    )
    parser.add_argument(
        "repo",
        nargs="?",
        help="Repo slug, e.g. 'langchain-ai/langchain'",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scrape every repo listed in scripts/repos.txt",
    )
    parser.add_argument(
        "--since",
        help="ISO datetime, only fetch issues updated after this (e.g. 2026-01-01)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help=(
            "Full historical backfill. Ignores --since and writes to "
            "`full-YYYY-MM-DD.jsonl` so daily incrementals can't clobber it."
        ),
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip Hugging Face upload; only write the local buffer",
    )
    args = parser.parse_args()

    if args.full and args.since:
        print(
            "NOTE: --full ignores --since (full backfill pulls all history).",
            file=sys.stderr,
        )
        args.since = None

    if args.all:
        repos = load_repos_file()
        if not repos:
            print(f"No repos listed in {REPOS_FILE}. Add one per line.", file=sys.stderr)
            return 2
    elif args.repo:
        repos = [args.repo]
    else:
        parser.print_help()
        return 2

    upload = not args.no_upload
    if upload and not hf_configured():
        print(
            f"NOTE: {HF_TOKEN_ENV} and/or {HF_REPO_ENV} not set.\n"
            "      Scraping locally only. Use --no-upload to silence this, or set both env vars.",
            file=sys.stderr,
        )
        upload = False

    total = 0
    for repo in repos:
        try:
            total += scrape(repo, args.since, upload, args.full)
        except (HTTPError, URLError) as e:
            print(f"[{repo}] failed: {e}", file=sys.stderr)
    mode = "full" if args.full else "incremental"
    print(f"done. {total} issues across {len(repos)} repo(s) ({mode}).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
