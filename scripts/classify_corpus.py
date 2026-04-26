#!/usr/bin/env python3
"""Bulk-classify GitHub issues against the Mycelium AF-* taxonomy with an LLM.

Two modes:

    # 1. Validate the classifier against the 50 hand-labeled issues.
    #    Prints accuracy report, does NOT write predictions.
    python scripts/classify_corpus.py validate

    # 2. Classify the full HF corpus (~15.8k issues). Resumable.
    python scripts/classify_corpus.py full --limit 100   # smoke test
    python scripts/classify_corpus.py full               # the real run

Output (full mode):
    incidents/tagged/v1/predictions.jsonl   one row per issue, append-only

Cost estimate (gpt-4o-mini): ~$0.0006/issue, ~$10 for the full 15.8k.

Auth: set OPENAI_API_KEY in env or in a `.env` file at repo root.
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
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_DIR = REPO_ROOT / "incidents" / "tagged"
V0_DIR = TAXONOMY_DIR / "v0"
V1_DIR = TAXONOMY_DIR / "v1"
PREDICTIONS = V1_DIR / "predictions.jsonl"

DEFAULT_HF_REPO = "ndileep/mycelium-agent-failures"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_CONCURRENCY = 20
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
# Taxonomy (load from disk so the spec files are the single source of truth)
# -----------------------------------------------------------------------------


def load_taxonomy_text() -> str:
    """Build a compact, model-readable taxonomy block from incidents/tagged/AF-*.md."""
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


# -----------------------------------------------------------------------------
# Prompt
# -----------------------------------------------------------------------------


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
# OpenAI call
# -----------------------------------------------------------------------------


async def classify_one(client, system_prompt: str, issue: dict[str, Any], model: str, max_retries: int = 6) -> dict[str, Any]:
    """Returns {label, confidence, evidence, reasoning} or {error: ...}.

    Retries with exponential backoff on rate limits / transient API errors.
    """
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
                or "5" in str(getattr(e, "status_code", ""))[:1]  # 5xx
            )
            if not is_retryable or attempt == max_retries - 1:
                break
            # Try to honor server-suggested wait time if present.
            wait = 2 ** attempt
            m = re.search(r"try again in (\d+(?:\.\d+)?)\s*(s|ms)", err_text)
            if m:
                val = float(m.group(1))
                wait = val / 1000 if m.group(2) == "ms" else val
                wait += 0.5  # cushion
            await asyncio.sleep(min(wait, 60))
    return {"error": f"{type(last_err).__name__}: {last_err}"}


# -----------------------------------------------------------------------------
# Loaders
# -----------------------------------------------------------------------------


def load_env() -> None:
    """Load .env file if present so OPENAI_API_KEY can live there."""
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
    except ImportError:
        pass


def get_client():
    if not os.environ.get("OPENAI_API_KEY"):
        print("ERROR: OPENAI_API_KEY not set.", file=sys.stderr)
        print("  Put it in .env at repo root:  echo 'OPENAI_API_KEY=sk-...' >> .env", file=sys.stderr)
        sys.exit(2)
    from openai import AsyncOpenAI
    return AsyncOpenAI()


def load_already_classified() -> set[str]:
    if not PREDICTIONS.exists():
        return set()
    ids: set[str] = set()
    for line in PREDICTIONS.read_text().splitlines():
        if line.strip():
            try:
                ids.add(json.loads(line)["id"])
            except (json.JSONDecodeError, KeyError):
                pass
    return ids


def append_prediction(entry: dict[str, Any]) -> None:
    PREDICTIONS.parent.mkdir(parents=True, exist_ok=True)
    with PREDICTIONS.open("a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


# -----------------------------------------------------------------------------
# Validate mode — run on the 50 hand-labeled, report agreement
# -----------------------------------------------------------------------------


def load_validation_set() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Returns list of (issue_dict, gold_label_dict)."""
    if not (V0_DIR / "queue.jsonl").exists():
        print("ERROR: incidents/tagged/v0/queue.jsonl not found", file=sys.stderr)
        sys.exit(2)
    if not (V0_DIR / "tagged.jsonl").exists():
        print("ERROR: incidents/tagged/v0/tagged.jsonl not found", file=sys.stderr)
        sys.exit(2)

    queue = {}
    for line in (V0_DIR / "queue.jsonl").read_text().splitlines():
        if line.strip():
            d = json.loads(line)
            queue[d["id"]] = d

    tagged = []
    for line in (V0_DIR / "tagged.jsonl").read_text().splitlines():
        if line.strip():
            tagged.append(json.loads(line))

    pairs = []
    for t in tagged:
        if t["id"] in queue:
            pairs.append((queue[t["id"]], t))
    return pairs


def normalize_gold(gold: dict[str, Any]) -> str:
    """Convert gold-tagged.jsonl entry to comparable label string."""
    if gold["status"] == "not-a-failure":
        return "n"
    if gold["status"] == "skip":
        return "skip"
    labels = gold.get("labels") or []
    return "+".join(sorted(labels))


def normalize_pred(pred: dict[str, Any]) -> str:
    label = (pred.get("label") or "").strip()
    if label.lower() in ("n", "not-a-failure", "none"):
        return "n"
    parts = sorted([p.strip() for p in label.split("+") if p.strip()])
    return "+".join(parts) if parts else "n"


async def run_validate(model: str, concurrency: int) -> int:
    load_env()
    client = get_client()
    system_prompt = build_system_prompt()
    pairs = load_validation_set()
    pairs = [(i, g) for i, g in pairs if normalize_gold(g) != "skip"]

    print(f"validation set: {len(pairs)} hand-labeled issues")
    print(f"model: {model}, concurrency: {concurrency}")
    print()

    sem = asyncio.Semaphore(concurrency)

    async def task(issue, gold):
        async with sem:
            t0 = time.monotonic()
            pred = await classify_one(client, system_prompt, issue, model)
            elapsed = time.monotonic() - t0
            return issue, gold, pred, elapsed

    results = await asyncio.gather(*[task(i, g) for i, g in pairs])

    correct = 0
    af_correct = 0
    af_total = 0
    n_correct = 0
    n_total = 0
    disagreements: list[tuple[dict, dict, dict]] = []

    for issue, gold, pred, _ in results:
        if "error" in pred:
            print(f"  ERROR on {issue['id']}: {pred['error']}")
            continue
        gold_norm = normalize_gold(gold)
        pred_norm = normalize_pred(pred)
        is_correct = gold_norm == pred_norm
        if gold_norm == "n":
            n_total += 1
            if is_correct:
                n_correct += 1
        else:
            af_total += 1
            if is_correct:
                af_correct += 1
        if is_correct:
            correct += 1
        else:
            disagreements.append((issue, gold, pred))

    total = len(results)
    print(f"OVERALL AGREEMENT: {correct}/{total} = {100 * correct / total:.1f}%")
    print(f"  on AF-tagged:    {af_correct}/{af_total} = {(100 * af_correct / af_total) if af_total else 0:.1f}%")
    print(f"  on not-a-failure:{n_correct}/{n_total} = {(100 * n_correct / n_total) if n_total else 0:.1f}%")
    print()

    if disagreements:
        print(f"DISAGREEMENTS ({len(disagreements)}):")
        for issue, gold, pred in disagreements:
            gold_norm = normalize_gold(gold)
            pred_norm = normalize_pred(pred)
            print(f"  {issue['id']}")
            print(f"    title:    {issue['title'][:80]}")
            print(f"    HUMAN:    {gold_norm}  (notes: {gold.get('notes', '')[:80]})")
            print(f"    LLM:      {pred_norm}  ({pred.get('confidence')})  ev: {pred.get('evidence', '')[:80]}")
            print()

    cost_per_call = 0.0006
    print(f"approx cost: ${total * cost_per_call:.3f} ({total} calls)")
    return 0


# -----------------------------------------------------------------------------
# Full mode — classify everything in the HF corpus
# -----------------------------------------------------------------------------


def load_full_corpus() -> list[dict[str, Any]]:
    """Download or read all `full-*.jsonl` files for each repo from HF cache.

    Returns a flat list of issue dicts with id, repo, etc."""
    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError:
        print("ERROR: huggingface_hub not installed.", file=sys.stderr)
        sys.exit(2)

    repo_id = (os.environ.get("MYCELIUM_HF_REPO") or DEFAULT_HF_REPO).strip()
    if not os.environ.get("HF_TOKEN"):
        print("ERROR: HF_TOKEN not set in env or .env", file=sys.stderr)
        sys.exit(2)

    print(f"[hf] reading {repo_id}", file=sys.stderr)
    api = HfApi()
    cache = REPO_ROOT / ".cache" / "hf-dl"
    cache.mkdir(parents=True, exist_ok=True)

    issues: list[dict[str, Any]] = []
    for repo in REPOS:
        _, name = repo.split("/", 1)
        tree = list(api.list_repo_tree(repo_id, path_in_repo=f"github-issues/{name}", repo_type="dataset"))
        full_files = sorted([f for f in tree if "full-" in f.path and f.path.endswith(".jsonl")])
        if not full_files:
            print(f"  [{repo}] no full-*.jsonl, skipping", file=sys.stderr)
            continue
        target = full_files[-1]
        local = hf_hub_download(repo_id, target.path, repo_type="dataset", local_dir=str(cache))
        with open(local) as fh:
            for line in fh:
                if not line.strip():
                    continue
                d = json.loads(line)
                if "pull_request" in d:
                    continue
                issues.append({
                    "id": f"{repo}#{d['number']}",
                    "repo": repo,
                    "number": d["number"],
                    "url": d.get("html_url", ""),
                    "title": d.get("title", ""),
                    "body": d.get("body") or "",
                    "state": d.get("state", "?"),
                    "labels": [lbl["name"] for lbl in d.get("labels", [])],
                })
        print(f"  [{repo}] {len([i for i in issues if i['repo'] == repo])} issues loaded", file=sys.stderr)

    return issues


async def run_full(model: str, concurrency: int, limit: int | None) -> int:
    load_env()
    client = get_client()
    system_prompt = build_system_prompt()
    issues = load_full_corpus()

    done = load_already_classified()
    todo = [i for i in issues if i["id"] not in done]
    if limit:
        todo = todo[:limit]

    print(f"corpus:           {len(issues)} issues")
    print(f"already done:     {len(done)}")
    print(f"to classify now:  {len(todo)}")
    print(f"model:            {model}, concurrency: {concurrency}")
    print(f"approx cost:      ${len(todo) * 0.0006:.2f}")
    print()
    if not todo:
        print("nothing to do.")
        return 0

    sem = asyncio.Semaphore(concurrency)
    completed = 0
    errors = 0
    counts: Counter[str] = Counter()
    t_start = time.monotonic()
    lock = asyncio.Lock()

    async def task(issue: dict[str, Any]) -> None:
        nonlocal completed, errors
        async with sem:
            pred = await classify_one(client, system_prompt, issue, model)
            entry = {"id": issue["id"], "repo": issue["repo"], "number": issue["number"], "url": issue["url"], "title": issue["title"]}
            if "error" in pred:
                async with lock:
                    errors += 1
                entry["error"] = pred["error"]
            else:
                entry.update({
                    "label": pred.get("label"),
                    "confidence": pred.get("confidence"),
                    "evidence": pred.get("evidence"),
                    "reasoning": pred.get("reasoning"),
                })
                async with lock:
                    counts[normalize_pred(pred)] += 1
            async with lock:
                append_prediction(entry)
                completed += 1
                if completed % 50 == 0 or completed == len(todo):
                    rate = completed / (time.monotonic() - t_start)
                    eta = (len(todo) - completed) / rate if rate else 0
                    print(f"  [{completed}/{len(todo)}] rate={rate:.1f}/s eta={eta:.0f}s errors={errors}", file=sys.stderr)

    await asyncio.gather(*[task(i) for i in todo])

    elapsed = time.monotonic() - t_start
    print()
    print(f"done in {elapsed:.0f}s, errors={errors}")
    print()
    print("label distribution (this run):")
    for k, v in counts.most_common():
        print(f"  {k:20s} {v}")
    return 0


# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("validate", help="Test the classifier against the 50 hand-labeled issues.")
    pv.add_argument("--model", default=DEFAULT_MODEL)
    pv.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)

    pf = sub.add_parser("full", help="Classify the full HF corpus. Resumable.")
    pf.add_argument("--model", default=DEFAULT_MODEL)
    pf.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    pf.add_argument("--limit", type=int, default=None, help="Cap the number of issues classified this run.")

    args = parser.parse_args()
    if args.cmd == "validate":
        return asyncio.run(run_validate(args.model, args.concurrency))
    if args.cmd == "full":
        return asyncio.run(run_full(args.model, args.concurrency, args.limit))
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
