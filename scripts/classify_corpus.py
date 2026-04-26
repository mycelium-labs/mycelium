#!/usr/bin/env python3
"""Classify the GitHub-issue corpus against the Mycelium AF-* taxonomy.

The Hugging Face dataset is the source of truth in both directions:

    raw issues:    github-issues/<repo>/...               (written by scrape_github_issues.py)
    predictions:   predictions/<repo>.jsonl               (written by this script)
                   predictions/manifest.json              (per-repo classifier state)

Each line in `predictions/<repo>.jsonl` is one issue's classification, keyed
by `id` (e.g. "langchain-ai/langchain#34906"). Append-only and idempotent:
re-running won't re-classify what's already there.

Commands:

    # Validate the classifier against the v0 hand-labeled set. No writes.
    python scripts/classify_corpus.py validate

    # Classify everything that hasn't been classified yet (default mode).
    # Pulls raw issues from HF, classifies new ids, pushes predictions to HF.
    # Safe to run repeatedly; this is what the daily GitHub Actions cron uses.
    python scripts/classify_corpus.py run

    # Same as `run` but skip the HF push (local-only smoke test).
    python scripts/classify_corpus.py run --no-push --limit 50

    # Restrict to a single repo (handy for debugging).
    python scripts/classify_corpus.py run --repo langchain-ai/langchain

Auth:
    OPENAI_API_KEY     classifier (.env or env var)
    HF_TOKEN           Hugging Face read+write
    MYCELIUM_HF_REPO   dataset slug, e.g. "ndileep/mycelium-agent-failures"

Cost (gpt-4o-mini): ~$0.0006/issue. Full ~15.8k corpus ≈ $10. Daily increments
of a few dozen new issues are pennies.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_DIR = REPO_ROOT / "incidents" / "tagged"
V0_DIR = TAXONOMY_DIR / "v0"
HF_CACHE = REPO_ROOT / ".cache" / "hf-dl"
LOCAL_PRED_CACHE = REPO_ROOT / ".cache" / "predictions"

DEFAULT_HF_REPO = "ndileep/mycelium-agent-failures"
DEFAULT_MODEL = "gpt-4o-mini"
# Default tuned for OpenAI Tier 1 (no payment method). Bump higher locally if
# you've upgraded — `--concurrency 20` will fly through ~15k issues in minutes.
DEFAULT_CONCURRENCY = 3
BODY_TRUNCATE_CHARS = 4000

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
# Taxonomy / prompt
# -----------------------------------------------------------------------------


def load_taxonomy_text() -> str:
    parts: list[str] = []
    for path in sorted(TAXONOMY_DIR.glob("AF-*.md")):
        text = path.read_text()
        m_name = re.match(r"#\s*(AF-\d+)\s*[—-]\s*(.+)", text.splitlines()[0])
        if not m_name:
            continue
        af_id, name = m_name.group(1), m_name.group(2).strip()
        oneline = re.search(r"\*\*One-line:\*\*\s*(.+)", text)
        signal = re.search(r"\*\*Detection signal:\*\*\s*(.+)", text)
        out_of = re.search(r"\*\*Out of scope[^\n]*\n((?:- .+\n?)+)", text, re.MULTILINE)
        block = [f"{af_id} — {name}"]
        if oneline:
            block.append(f"  meaning: {oneline.group(1).strip()}")
        if signal:
            block.append(f"  signal:  {signal.group(1).strip()}")
        if out_of:
            block.append(f"  NOT this if: {out_of.group(1).strip().replace(chr(10), '; ')}")
        parts.append("\n".join(block))
    return "\n\n".join(parts)


def build_system_prompt() -> str:
    taxonomy = load_taxonomy_text()
    return f"""You are a triage classifier for the Mycelium AF-* failure-mode taxonomy.

You receive a single GitHub issue from a popular AI-agent framework repository
(LangChain, LangGraph, AutoGen, CrewAI, OpenAI Agents SDK, smolagents,
OpenHands, Cline, Stagehand, LiveKit Agents).

Your job: decide whether the issue describes (or provides structural mechanism
evidence of) one of these AGENT BEHAVIORAL failure modes:

{taxonomy}

CRITICAL DECISION RULES:

1. The issue must be about AGENT BEHAVIOR, not generic library/runtime/install
   problems. Tag "n" (not-a-failure) for:
   - feature requests, "ENH:", "fr:", "RFC:" titles asking for new functionality
   - typos, docs fixes, "fix comment" PR-shaped issues
   - install / environment / dependency / version-mismatch errors
   - "How do I..." usage questions with no failure
   - vendor pitches and integration proposals from third parties
   - UI / cosmetic / formatting bugs in the framework's surface
   - loud crashes, raised exceptions, missing retry logic — runtime plumbing,
     covered by Sentry-class tools, not Mycelium
   - library API design ambiguity (return-type bugs, config bugs) when the
     agent's behavior wasn't reported as wrong

2. Tag an AF-* label when the issue describes (or shows code evidence of) the
   agent doing the wrong thing in its decision/plan/tool/memory/observability
   layer. Two evidence kinds count:
   - INCIDENT: a user reports the agent behaving wrong, with repro
   - MECHANISM: code points to a specific structural cause that would produce
     the failure (e.g. "except: pass in tool exec" → AF-002)

3. Multiple labels are allowed if a single issue spans modes (e.g. a memory
   poisoning attack maps to both AF-009 and AF-006).

4. Confidence:
   - high:   reproducible incident with clear repro, OR explicit code refs
             showing the mechanism
   - medium: clear mechanism but no incident, OR incident but partial repro
   - low:    plausible match but evidence weak (suspicious repro, hypothetical
             RFC, or label-by-resemblance)

5. For "n" verdicts, give the rejection category in `evidence` (e.g.
   "feature request", "library bug", "install issue", "vendor pitch",
   "loud crash / runtime plumbing").

6. Be skeptical. Most issues will be "n". The signal is rare and that's fine.
"""


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {
            "type": "string",
            "description": "Either 'n' (not a failure) or one or more AF-* tags joined by '+', e.g. 'AF-006' or 'AF-009+AF-006'",
        },
        "confidence": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "description": "Confidence in the classification. For 'n' labels, set to 'high' unless ambiguous.",
        },
        "evidence": {
            "type": "string",
            "description": "One-line direct quote or specific code reference supporting the label. For 'n' labels, the rejection category.",
        },
        "reasoning": {
            "type": "string",
            "description": "One-sentence explanation of why this label fits.",
        },
    },
    "required": ["label", "confidence", "evidence", "reasoning"],
    "additionalProperties": False,
}


def build_user_message(issue: dict[str, Any]) -> str:
    body = (issue.get("body") or "").strip()
    if len(body) > BODY_TRUNCATE_CHARS:
        body = body[:BODY_TRUNCATE_CHARS] + "\n[... truncated]"
    labels = ", ".join(issue.get("labels", [])[:6])
    return f"""REPO: {issue['repo']}
ISSUE #{issue['number']} ({issue.get('state', '?')})
URL: {issue.get('url') or issue.get('html_url', '')}
GITHUB LABELS: {labels or '(none)'}

TITLE: {issue['title']}

BODY:
{body or '(no body)'}
"""


# -----------------------------------------------------------------------------
# OpenAI call w/ exponential backoff
# -----------------------------------------------------------------------------


async def classify_one(client, system_prompt: str, issue: dict[str, Any], model: str, max_retries: int = 6) -> dict[str, Any]:
    user_msg = build_user_message(issue)
    last_err: Exception | None = None
    for attempt in range(max_retries):
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_msg},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "af_classification",
                        "strict": True,
                        "schema": CLASSIFY_SCHEMA,
                    },
                },
                temperature=0.0,
            )
            content = resp.choices[0].message.content
            return json.loads(content) if content else {"error": "empty response"}
        except Exception as e:
            last_err = e
            err_text = str(e).lower()
            is_retryable = (
                "rate limit" in err_text
                or "429" in err_text
                or "timeout" in err_text
                or "connection" in err_text
                or "5" in str(getattr(e, "status_code", ""))[:1]
            )
            if not is_retryable or attempt == max_retries - 1:
                break
            wait = 2 ** attempt
            m = re.search(r"try again in (\d+(?:\.\d+)?)\s*(s|ms)", err_text)
            if m:
                val = float(m.group(1))
                wait = val / 1000 if m.group(2) == "ms" else val
                wait += 0.5
            await asyncio.sleep(min(wait, 60))
    return {"error": f"{type(last_err).__name__}: {last_err}"}


# -----------------------------------------------------------------------------
# Env / clients
# -----------------------------------------------------------------------------


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass


def get_openai_client():
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        print("  Put it in .env at repo root:  echo 'OPENAI_API_KEY=sk-...' >> .env", file=sys.stderr)
        sys.exit(2)
    from openai import AsyncOpenAI
    return AsyncOpenAI()


def get_hf_api():
    try:
        from huggingface_hub import HfApi, get_token
    except ImportError:
        print("ERROR: huggingface_hub not installed. uv pip install huggingface_hub", file=sys.stderr)
        sys.exit(2)
    token = os.environ.get("HF_TOKEN") or get_token()
    if not token:
        print("ERROR: no HF token found. Set HF_TOKEN env var or run `huggingface-cli login`.", file=sys.stderr)
        sys.exit(2)
    return HfApi(token=token)


def hf_repo_id() -> str:
    return (os.environ.get("MYCELIUM_HF_REPO") or DEFAULT_HF_REPO).strip()


# -----------------------------------------------------------------------------
# Read raw issues from HF (union of all files for a repo, deduped by id)
# -----------------------------------------------------------------------------


def load_raw_issues_for_repo(api, repo_id: str, repo: str) -> list[dict[str, Any]]:
    """Download all `*.jsonl` files for a repo from HF and dedupe by issue number.

    Later files (more recent dates) win on conflict.
    """
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import EntryNotFoundError

    _, name = repo.split("/", 1)
    HF_CACHE.mkdir(parents=True, exist_ok=True)

    try:
        tree = list(api.list_repo_tree(repo_id, path_in_repo=f"github-issues/{name}", repo_type="dataset"))
    except EntryNotFoundError:
        return []

    files = sorted([f.path for f in tree if f.path.endswith(".jsonl")])
    if not files:
        return []

    by_number: dict[int, dict[str, Any]] = {}
    for path in files:
        local = hf_hub_download(repo_id, path, repo_type="dataset", local_dir=str(HF_CACHE))
        with open(local) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if "pull_request" in d:
                    continue
                num = d.get("number")
                if num is None:
                    continue
                by_number[num] = d  # later file wins

    out = []
    for d in by_number.values():
        out.append({
            "id": f"{repo}#{d['number']}",
            "repo": repo,
            "number": d["number"],
            "url": d.get("html_url", ""),
            "title": d.get("title", ""),
            "body": d.get("body") or "",
            "state": d.get("state", "?"),
            "labels": [lbl["name"] for lbl in d.get("labels", [])],
        })
    return out


# -----------------------------------------------------------------------------
# Read existing predictions for a repo (so we don't reclassify)
# -----------------------------------------------------------------------------


def hf_predictions_path(repo: str) -> str:
    _, name = repo.split("/", 1)
    return f"predictions/{name}.jsonl"


def local_predictions_path(repo: str) -> Path:
    _, name = repo.split("/", 1)
    return LOCAL_PRED_CACHE / f"{name}.jsonl"


def download_existing_predictions(api, repo_id: str, repo: str) -> list[dict[str, Any]]:
    """Pull the current predictions/<repo>.jsonl from HF (or empty if first run)."""
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import EntryNotFoundError

    LOCAL_PRED_CACHE.mkdir(parents=True, exist_ok=True)
    try:
        local = hf_hub_download(
            repo_id,
            hf_predictions_path(repo),
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


# -----------------------------------------------------------------------------
# Push predictions back to HF
# -----------------------------------------------------------------------------


_PREDICTIONS_CARD_NOTE = """\
Predictions written by `scripts/classify_corpus.py` against the AF-* taxonomy
defined in the Mycelium repo. One file per source repo, append-only.

Schema per line:
  id           e.g. "langchain-ai/langchain#34906"
  repo, number, url, title
  label        "n" (not a failure) | "AF-XXX" | "AF-XXX+AF-YYY"
  confidence   "high" | "medium" | "low"
  evidence     one-line quote or rejection category
  reasoning    one-sentence justification
  model        classifier model used
  classified_at  ISO-8601 UTC timestamp
"""


def push_predictions(
    api,
    repo_id: str,
    repo: str,
    rows: list[dict[str, Any]],
    manifest: dict[str, Any],
) -> None:
    from huggingface_hub import CommitOperationAdd

    body_bytes = ("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n").encode("utf-8")
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")

    operations = [
        CommitOperationAdd(
            path_in_repo=hf_predictions_path(repo),
            path_or_fileobj=body_bytes,
        ),
        CommitOperationAdd(
            path_in_repo="predictions/manifest.json",
            path_or_fileobj=manifest_bytes,
        ),
    ]
    _, name = repo.split("/", 1)
    api.create_commit(
        repo_id=repo_id,
        repo_type="dataset",
        operations=operations,
        commit_message=f"classify: {name} +{manifest[repo]['delta']} (total {manifest[repo]['count']})",
    )


def write_predictions_local(repo: str, rows: list[dict[str, Any]]) -> Path:
    p = local_predictions_path(repo)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def fetch_manifest(api, repo_id: str) -> dict[str, Any]:
    from huggingface_hub import hf_hub_download
    from huggingface_hub.utils import EntryNotFoundError

    try:
        local = hf_hub_download(
            repo_id,
            "predictions/manifest.json",
            repo_type="dataset",
            local_dir=str(LOCAL_PRED_CACHE / "_hf"),
        )
        return json.loads(Path(local).read_text())
    except EntryNotFoundError:
        return {}


# -----------------------------------------------------------------------------
# Orchestration: `run` mode
# -----------------------------------------------------------------------------


async def classify_repo(
    api,
    repo_id: str,
    repo: str,
    model: str,
    concurrency: int,
    limit: int | None,
    push: bool,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Classify all unclassified issues in a single repo. Returns stats."""
    print(f"\n=== {repo} ===", file=sys.stderr)

    raw_issues = load_raw_issues_for_repo(api, repo_id, repo)
    if not raw_issues:
        print(f"  no raw issues on HF for {repo}, skipping", file=sys.stderr)
        return {"repo": repo, "skipped": True}

    existing = download_existing_predictions(api, repo_id, repo)
    done_ids = {r["id"] for r in existing}

    todo = [i for i in raw_issues if i["id"] not in done_ids]
    if limit:
        todo = todo[:limit]

    print(f"  raw: {len(raw_issues)}  already classified: {len(done_ids)}  to classify: {len(todo)}", file=sys.stderr)

    if not todo:
        return {"repo": repo, "raw": len(raw_issues), "delta": 0, "errors": 0}

    client = get_openai_client()
    system_prompt = build_system_prompt()
    sem = asyncio.Semaphore(concurrency)
    new_rows: list[dict[str, Any]] = []
    errors = 0
    error_samples: list[str] = []
    counts: Counter[str] = Counter()
    completed = 0
    successes = 0
    t0 = time.monotonic()
    lock = asyncio.Lock()
    abort_event = asyncio.Event()

    # Fail-fast guardrail: if every one of the first FAIL_FAST_THRESHOLD
    # calls errors out (zero successes), abort the whole run instead of
    # grinding through hundreds of retries. Caught us once when a free-tier
    # quota was exhausted and the workflow burned 90 minutes on 429s.
    FAIL_FAST_THRESHOLD = 15

    async def task(issue: dict[str, Any]) -> None:
        nonlocal completed, errors, successes
        if abort_event.is_set():
            return
        async with sem:
            if abort_event.is_set():
                return
            pred = await classify_one(client, system_prompt, issue, model)
            row = {
                "id": issue["id"],
                "repo": issue["repo"],
                "number": issue["number"],
                "url": issue["url"],
                "title": issue["title"],
                "model": model,
                "classified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            if "error" in pred:
                row["error"] = pred["error"]
                async with lock:
                    errors += 1
                    if not error_samples:
                        # Surface the *first* error immediately so a stuck run
                        # is debuggable from the live log, not just at the end.
                        print(f"  first error: {pred['error']}", file=sys.stderr)
                    if len(error_samples) < 3:
                        error_samples.append(pred["error"])
                    if errors >= FAIL_FAST_THRESHOLD and successes == 0:
                        if not abort_event.is_set():
                            print(
                                f"  ABORT: {errors} consecutive failures with zero successes.\n"
                                f"  This usually means an OpenAI quota was hit (RPD/TPM) or the\n"
                                f"  API key is wrong. Check https://platform.openai.com/account/limits\n"
                                f"  before re-running. Pipeline is idempotent — already-classified\n"
                                f"  issues will be skipped.",
                                file=sys.stderr,
                            )
                        abort_event.set()
            else:
                row.update({
                    "label": pred.get("label"),
                    "confidence": pred.get("confidence"),
                    "evidence": pred.get("evidence"),
                    "reasoning": pred.get("reasoning"),
                })
                async with lock:
                    successes += 1
                    counts[(pred.get("label") or "?").lower()] += 1
            async with lock:
                new_rows.append(row)
                completed += 1
                if completed % 50 == 0 or completed == len(todo):
                    rate = completed / (time.monotonic() - t0)
                    eta = (len(todo) - completed) / rate if rate else 0
                    print(f"  [{completed}/{len(todo)}] rate={rate:.1f}/s eta={eta:.0f}s ok={successes} errors={errors}", file=sys.stderr)

    await asyncio.gather(*[task(i) for i in todo])
    if abort_event.is_set():
        print(f"  aborted after {completed} attempts, {errors} errors, {successes} successes", file=sys.stderr)

    successful = [r for r in new_rows if "error" not in r]
    failed = [r for r in new_rows if "error" in r]
    merged = existing + successful

    if failed:
        print(f"  {len(failed)} failures dropped from final file (will retry on next run)", file=sys.stderr)
        for sample in error_samples:
            print(f"    sample error: {sample}", file=sys.stderr)

    if not successful:
        print(f"  no successful classifications this run — skipping HF push", file=sys.stderr)
        return {"repo": repo, "raw": len(raw_issues), "delta": 0, "errors": errors,
                "aborted": abort_event.is_set()}

    write_predictions_local(repo, merged)

    manifest[repo] = {
        "count": len(merged),
        "delta": len(successful),
        "last_classified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
    }

    if push:
        try:
            push_predictions(api, repo_id, repo, merged, manifest)
            print(f"  [hf] pushed predictions/{repo.split('/', 1)[1]}.jsonl ({len(merged)} rows, +{len(successful)} new)", file=sys.stderr)
        except Exception as e:
            print(f"  [hf] push failed: {e}", file=sys.stderr)
            return {"repo": repo, "raw": len(raw_issues), "delta": len(successful), "errors": errors, "push_error": str(e)}

    return {"repo": repo, "raw": len(raw_issues), "delta": len(successful), "errors": errors}


async def run_corpus(
    model: str,
    concurrency: int,
    limit: int | None,
    max_issues: int | None,
    push: bool,
    only_repo: str | None,
) -> int:
    load_env()
    api = get_hf_api()
    repo_id = hf_repo_id()
    print(f"[hf] dataset: {repo_id}", file=sys.stderr)

    manifest = fetch_manifest(api, repo_id)

    targets = REPOS if only_repo is None else [only_repo]
    if only_repo and only_repo not in REPOS:
        print(f"WARNING: {only_repo} not in REPOS list, classifying anyway", file=sys.stderr)

    stats = []
    remaining_budget = max_issues
    for repo in targets:
        if remaining_budget is not None and remaining_budget <= 0:
            print(f"\n=== {repo} === skipped: --max-issues budget exhausted", file=sys.stderr)
            stats.append({"repo": repo, "skipped": True, "reason": "budget"})
            continue
        per_repo_limit = limit
        if remaining_budget is not None:
            per_repo_limit = remaining_budget if per_repo_limit is None else min(per_repo_limit, remaining_budget)
        try:
            s = await classify_repo(api, repo_id, repo, model, concurrency, per_repo_limit, push, manifest)
            stats.append(s)
            if remaining_budget is not None:
                remaining_budget -= s.get("delta", 0)
            # If a repo aborted (quota exhausted), bail on the rest of the run.
            if s.get("aborted"):
                print(f"\n=== aborting remaining repos: {repo} aborted on quota ===", file=sys.stderr)
                for skipped in targets[targets.index(repo) + 1:]:
                    stats.append({"repo": skipped, "skipped": True, "reason": "abort"})
                break
        except Exception as e:
            print(f"[{repo}] failed: {e}", file=sys.stderr)
            stats.append({"repo": repo, "error": str(e)})

    print("\n=== summary ===", file=sys.stderr)
    total_delta = 0
    for s in stats:
        if s.get("skipped"):
            reason = s.get("reason", "no raw")
            print(f"  {s['repo']}: skipped ({reason})", file=sys.stderr)
        elif "error" in s:
            print(f"  {s['repo']}: ERROR {s['error']}", file=sys.stderr)
        else:
            print(f"  {s['repo']}: +{s.get('delta', 0)} new (raw={s.get('raw', 0)}, errors={s.get('errors', 0)})", file=sys.stderr)
            total_delta += s.get("delta", 0)
    print(f"total newly classified: {total_delta}", file=sys.stderr)
    return 0


# -----------------------------------------------------------------------------
# Validate mode (against v0 hand-labeled set)
# -----------------------------------------------------------------------------


def load_validation_set() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if not (V0_DIR / "queue.jsonl").exists() or not (V0_DIR / "tagged.jsonl").exists():
        print("ERROR: missing v0/queue.jsonl or v0/tagged.jsonl", file=sys.stderr)
        sys.exit(2)
    queue = {json.loads(line)["id"]: json.loads(line) for line in (V0_DIR / "queue.jsonl").read_text().splitlines() if line.strip()}
    tagged = [json.loads(line) for line in (V0_DIR / "tagged.jsonl").read_text().splitlines() if line.strip()]
    return [(queue[t["id"]], t) for t in tagged if t["id"] in queue]


def normalize_gold(gold: dict[str, Any]) -> str:
    if gold["status"] == "not-a-failure":
        return "n"
    if gold["status"] == "skip":
        return "skip"
    return "+".join(sorted(gold.get("labels") or []))


def normalize_pred(pred: dict[str, Any]) -> str:
    label = (pred.get("label") or "").strip()
    if label.lower() in ("n", "not-a-failure", "none"):
        return "n"
    parts = sorted([p.strip() for p in label.split("+") if p.strip()])
    return "+".join(parts) if parts else "n"


async def run_validate(model: str, concurrency: int) -> int:
    load_env()
    client = get_openai_client()
    system_prompt = build_system_prompt()
    pairs = [(i, g) for i, g in load_validation_set() if normalize_gold(g) != "skip"]

    print(f"validation set: {len(pairs)} hand-labeled issues")
    print(f"model: {model}, concurrency: {concurrency}\n")

    sem = asyncio.Semaphore(concurrency)

    async def task(issue, gold):
        async with sem:
            pred = await classify_one(client, system_prompt, issue, model)
            return issue, gold, pred

    results = await asyncio.gather(*[task(i, g) for i, g in pairs])

    correct = af_correct = af_total = n_correct = n_total = 0
    disagreements: list[tuple[dict, dict, dict]] = []

    for issue, gold, pred in results:
        if "error" in pred:
            print(f"  ERROR on {issue['id']}: {pred['error']}")
            continue
        gold_norm = normalize_gold(gold)
        pred_norm = normalize_pred(pred)
        is_correct = gold_norm == pred_norm
        if gold_norm == "n":
            n_total += 1
            n_correct += is_correct
        else:
            af_total += 1
            af_correct += is_correct
        correct += is_correct
        if not is_correct:
            disagreements.append((issue, gold, pred))

    total = len(results)
    print(f"OVERALL AGREEMENT: {correct}/{total} = {100 * correct / total:.1f}%")
    print(f"  on AF-tagged:    {af_correct}/{af_total} = {(100 * af_correct / af_total) if af_total else 0:.1f}%")
    print(f"  on not-a-failure:{n_correct}/{n_total} = {(100 * n_correct / n_total) if n_total else 0:.1f}%\n")

    if disagreements:
        print(f"DISAGREEMENTS ({len(disagreements)}):")
        for issue, gold, pred in disagreements:
            print(f"  {issue['id']}")
            print(f"    title:  {issue['title'][:80]}")
            print(f"    HUMAN:  {normalize_gold(gold)}  notes: {gold.get('notes', '')[:80]}")
            print(f"    LLM:    {normalize_pred(pred)}  ({pred.get('confidence')})  ev: {pred.get('evidence', '')[:80]}\n")

    print(f"approx cost: ${total * 0.0006:.3f} ({total} calls)")
    return 0


# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("validate", help="Validate against the v0 hand-labeled set.")
    pv.add_argument("--model", default=DEFAULT_MODEL)
    pv.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)

    pr = sub.add_parser("run", help="Classify any unclassified issues, push to HF.")
    pr.add_argument("--model", default=DEFAULT_MODEL)
    pr.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"Concurrent in-flight requests (default {DEFAULT_CONCURRENCY}, tuned for tier 1 limits).")
    pr.add_argument("--limit", type=int, default=None, help="Cap classifications per repo (smoke test).")
    pr.add_argument("--max-issues", type=int, default=None,
                    help="Cap total classifications across all repos this run. Useful in CI to stay under job timeout.")
    pr.add_argument("--repo", default=None, help="Only classify this one repo (e.g. langchain-ai/langchain).")
    pr.add_argument("--no-push", action="store_true", help="Skip HF upload (local-only run).")

    args = parser.parse_args()
    if args.cmd == "validate":
        return asyncio.run(run_validate(args.model, args.concurrency))
    if args.cmd == "run":
        return asyncio.run(run_corpus(
            model=args.model,
            concurrency=args.concurrency,
            limit=args.limit,
            max_issues=args.max_issues,
            push=not args.no_push,
            only_repo=args.repo,
        ))
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
