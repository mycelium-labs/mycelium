#!/usr/bin/env python3
"""Classify the GitHub-issue corpus against the Mycelium AF-* taxonomy.

The Hugging Face dataset is the source of truth in both directions:

    raw issues:    github-issues/<repo>/...               (written by scrape_github_issues.py)
    predictions:   predictions/<repo>.jsonl               (written by this script)
                   predictions/manifest.json              (per-repo classifier state)

Each line in `predictions/<repo>.jsonl` is one issue's classification, keyed
by `id` (e.g. "langchain-ai/langchain#34906"). Append-only and idempotent:
re-running won't re-classify what's already there.

**Failure-mode catalog:** the dataset on Hugging Face (`predictions/*.jsonl`) is
the product catalog — prefilter rows (`model: prefilter:*`) plus LLM rows
(`label`, `evidence`, etc.). There is no separate human-curated merge step.

Commands:

    # Optional: compare classifier + prefilter to the frozen v0 regression pairs.
    python scripts/classify_corpus.py validate

    # Classify everything that hasn't been classified yet (default mode).
    # Pulls raw issues from HF, classifies new ids, pushes predictions to HF.
    # Safe to run repeatedly; this is what the daily GitHub Actions cron uses.
    python scripts/classify_corpus.py run

    # Same as `run` but skip the HF push (local-only smoke test).
    python scripts/classify_corpus.py run --no-push --limit 50

    # Restrict to a single repo (handy for debugging).
    python scripts/classify_corpus.py run --repo langchain-ai/langchain

Auth (classifier — use one provider):
    GROQ_API_KEY       recommended: Groq OpenAI-compatible API, fast / cheap Llama
                       (https://console.groq.com). Default model: see GROQ_MODEL.
    ANTHROPIC_API_KEY  optional fallback if Groq is not set
    LLM_BACKEND        force `groq` or `anthropic` when both keys are present
    GROQ_MODEL         default: llama-3.1-8b-instant
    ANTHROPIC_MODEL    default: claude-haiku-4-5
    HF_TOKEN           Hugging Face read+write
    MYCELIUM_HF_REPO   dataset slug, e.g. "ndileep/mycelium-agent-failures"

Cost: Groq Llama 8B is roughly an order of magnitude cheaper than Claude Haiku
for this workload; exact spend depends on Groq pricing and prompt length.
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
# Groq: OpenAI-compatible endpoint, Llama (cheap). Override with GROQ_MODEL.
DEFAULT_GROQ_MODEL = "llama-3.1-8b-instant"
# Anthropic: used only when ANTHROPIC_API_KEY is set and Groq is not (unless LLM_BACKEND).
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_CONCURRENCY = 4  # Groq free/on-demand TPM is tight; raise with --concurrency on paid tier
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


# -----------------------------------------------------------------------------
# Deterministic pre-filter
#
# Most agent-framework GitHub issues are not behavioral failures — they're
# feature requests, docs PRs, install errors, vendor pitches, etc. We can
# regex-reject the most obvious ones and skip the LLM entirely.
#
# Design contract: ZERO false negatives against the v0 hand-tagged set. False
# positives (passing an `n` through to the LLM) are fine — the LLM handles
# them. Run `python scripts/classify_corpus.py validate-prefilter` to verify.
# -----------------------------------------------------------------------------

PREFILTER_RULES: list[tuple[str, "re.Pattern", str]] = [
    ("feature_prefix",
     re.compile(r"^\s*\[?(ENH|FR|FEAT|FEATURE|RFE)\]?\s*[:\-]", re.IGNORECASE),
     "feature request"),
    ("feature_explicit",
     re.compile(r"\bfeature\s+request\b|^\s*new\s+feature\s*[:\-]", re.IGNORECASE),
     "feature request"),
    ("tool_idea",
     re.compile(r"^\s*tool\s+idea\s*[:\-]", re.IGNORECASE),
     "tool idea / vendor pitch"),
    ("integration_pitch",
     re.compile(r"\[?integration\s*(proposal|idea)\]?|^\s*integration\s*[:\-]", re.IGNORECASE),
     "integration / vendor pitch"),
    ("docs_prefix",
     re.compile(r"^\s*\[?docs?\]?\s*[:\-]|^\s*documentation\s*[:\-]", re.IGNORECASE),
     "docs change"),
    ("typo_fix",
     re.compile(r"^\s*fix\s*[:\-]?\s*typo\b|\bfix\s+typo\b|\btypo\s+in\b", re.IGNORECASE),
     "typo fix"),
    ("roadmap",
     re.compile(r"^\s*\[(roadmap|epic)\]|^\s*(roadmap|epic)\s*[:\-]", re.IGNORECASE),
     "roadmap / epic"),
    ("how_to",
     re.compile(r"^\s*how\s+(to|do|can|does|did|should)\b", re.IGNORECASE),
     "usage question"),
    ("install_env",
     re.compile(r"\bpip\s+install\b|\bnpm\s+install\b|\binstallation\b|\bcannot\s+install\b|\bvsix\b|\bchocolatey\b|\bbrew\s+install\b", re.IGNORECASE),
     "install / env issue"),
    ("os_specific",
     re.compile(r"\[\s*(windows|mac\s?os|macos|linux)\s+specific\s*\]", re.IGNORECASE),
     "OS-specific issue"),
]


def apply_prefilter(issue: dict[str, Any]) -> tuple[str, str] | None:
    """Return (rule_name, human_category) if a rule fires, else None.

    Only inspects the title — body inspection is reserved for the LLM. This
    keeps false-negative risk low: titles are short and pattern-matchable;
    bodies are where the real failure mechanism shows up.
    """
    title = (issue.get("title") or "").strip()
    if len(title) < 5:
        return ("spam_short", "spam / empty title")
    for rule_name, pattern, category in PREFILTER_RULES:
        if pattern.search(title):
            return (rule_name, category)
    return None


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
# Anthropic (Claude) call w/ exponential backoff
# -----------------------------------------------------------------------------


def _strip_json_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        lines = t.split("\n")
        lines = lines[1:]
        while lines and lines[-1].strip() in ("```", ""):
            lines.pop()
        t = "\n".join(lines).strip()
    return t


def _parse_classification_json(text: str) -> dict[str, Any]:
    raw = _strip_json_fences(text)
    data = json.loads(raw)
    for k in ("label", "confidence", "evidence", "reasoning"):
        if k not in data:
            raise ValueError(f"missing key {k!r} in {data!r}")
    return data


async def classify_one(
    client: Any,
    system_prompt: str,
    issue: dict[str, Any],
    model: str,
    backend: str,
    max_retries: int = 6,
) -> dict[str, Any]:
    user_msg = build_user_message(issue)
    instruction = (
        "\n\nRespond with ONLY a single JSON object (no markdown code fences), "
        'with keys exactly: "label", "confidence", "evidence", "reasoning". '
        "Follow the schema described in the system prompt. No text before or after the JSON."
    )
    groq_json_mode = True  # prefer json_object on Groq; disable if API rejects it
    last_err: Exception | None = None

    for attempt in range(max_retries):
        try:
            if backend == "groq":
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg + instruction},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 2048,
                }
                if groq_json_mode:
                    kwargs["response_format"] = {"type": "json_object"}
                resp = await client.chat.completions.create(**kwargs)
                content = (resp.choices[0].message.content or "").strip()
                if not content:
                    return {"error": "empty response"}
                return _parse_classification_json(content)
            message = await client.messages.create(
                model=model,
                max_tokens=2048,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg + instruction}],
                temperature=0.0,
            )
            if not message.content:
                return {"error": "empty response"}
            block = message.content[0]
            if getattr(block, "type", None) != "text":
                return {"error": f"unexpected block type: {getattr(block, 'type', block)}"}
            return _parse_classification_json(block.text)
        except (json.JSONDecodeError, ValueError) as e:
            last_err = e
            if attempt == max_retries - 1:
                break
            await asyncio.sleep(min(2 ** attempt, 10))
        except Exception as e:
            last_err = e
            err_text = str(e).lower()
            if backend == "groq" and groq_json_mode and any(
                x in err_text for x in ("json_object", "response_format", "unsupported", "json mode")
            ):
                groq_json_mode = False
                continue
            is_retryable = (
                "rate limit" in err_text
                or "429" in err_text
                or "timeout" in err_text
                or "connection" in err_text
                or "overloaded" in err_text
                or "529" in err_text
                or str(getattr(e, "status_code", "")) in ("429", "503", "529")
            )
            if not is_retryable or attempt == max_retries - 1:
                break
            wait = float(min(2 ** attempt, 60))
            # Groq: "try again in 140ms" (no space before ms)
            m = re.search(r"try again in (\d+(?:\.\d+)?)\s*(ms|s)\b", err_text)
            if m:
                val = float(m.group(1))
                wait = val / 1000 if m.group(2) == "ms" else val
                wait += 0.15
            if backend == "groq" and ("429" in err_text or "rate limit" in err_text):
                wait = max(wait, 1.0)
            await asyncio.sleep(min(wait, 120))
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


def resolve_backend() -> str:
    """Prefer Groq when configured (cheaper)."""
    load_env()
    forced = (os.environ.get("LLM_BACKEND") or "").strip().lower()
    g = bool(os.environ.get("GROQ_API_KEY", "").strip())
    a = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    if forced == "groq":
        if not g:
            print("ERROR: LLM_BACKEND=groq but GROQ_API_KEY is not set.", file=sys.stderr)
            sys.exit(2)
        return "groq"
    if forced == "anthropic":
        if not a:
            print("ERROR: LLM_BACKEND=anthropic but ANTHROPIC_API_KEY is not set.", file=sys.stderr)
            sys.exit(2)
        return "anthropic"
    if g:
        return "groq"
    if a:
        return "anthropic"
    print("ERROR: Set GROQ_API_KEY (https://console.groq.com) and/or ANTHROPIC_API_KEY", file=sys.stderr)
    print("  Optional: LLM_BACKEND=groq|anthropic when both are set (default: groq).", file=sys.stderr)
    sys.exit(2)


def default_model_for_backend(backend: str) -> str:
    if backend == "groq":
        return os.environ.get("GROQ_MODEL", DEFAULT_GROQ_MODEL).strip() or DEFAULT_GROQ_MODEL
    return os.environ.get("ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL).strip() or DEFAULT_ANTHROPIC_MODEL


def cap_concurrency_for_backend(backend: str, concurrency: int) -> int:
    """Groq enforces TPM; too many parallel large prompts → 429 even with per-request retry."""
    if backend != "groq":
        return concurrency
    cap = max(1, int(os.environ.get("GROQ_MAX_CONCURRENCY", "4")))
    if concurrency > cap:
        print(
            f"[llm] Groq TPM: capping concurrency {concurrency} → {cap} "
            f"(env GROQ_MAX_CONCURRENCY or --concurrency; paid tier can use 8–16+)",
            file=sys.stderr,
        )
        return cap
    return concurrency


def get_llm_client() -> tuple[Any, str]:
    """Return (async client, backend name)."""
    backend = resolve_backend()
    if backend == "groq":
        key = os.environ.get("GROQ_API_KEY", "").strip()
        try:
            from openai import AsyncOpenAI
        except ImportError:
            print("ERROR: openai not installed. Run: uv pip install openai", file=sys.stderr)
            sys.exit(2)
        client = AsyncOpenAI(
            base_url="https://api.groq.com/openai/v1",
            api_key=key,
        )
        return client, "groq"
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        print("ERROR: anthropic not installed. Run: uv pip install anthropic", file=sys.stderr)
        sys.exit(2)
    return AsyncAnthropic(api_key=key), "anthropic"


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
    client: Any,
    backend: str,
    model: str,
    concurrency: int,
    limit: int | None,
    push: bool,
    manifest: dict[str, Any],
    prefilter_only: bool = False,
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

    # Deterministic pre-filter: catch the obvious 'n's without spending tokens.
    prefilter_rows: list[dict[str, Any]] = []
    llm_todo: list[dict[str, Any]] = []
    prefilter_breakdown: Counter[str] = Counter()
    prefilter_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for issue in todo:
        hit = apply_prefilter(issue)
        if hit:
            rule_name, category = hit
            prefilter_breakdown[rule_name] += 1
            prefilter_rows.append({
                "id": issue["id"],
                "repo": issue["repo"],
                "number": issue["number"],
                "url": issue["url"],
                "title": issue["title"],
                "model": f"prefilter:{rule_name}",
                "classified_at": prefilter_ts,
                "label": "n",
                "confidence": "high",
                "evidence": f"prefilter: {category}",
                "reasoning": f"matched deterministic rule '{rule_name}' on title",
            })
        else:
            llm_todo.append(issue)

    print(f"  prefilter: caught {len(prefilter_rows)} / {len(todo)} ({100 * len(prefilter_rows) / max(len(todo), 1):.0f}%) → LLM sees {len(llm_todo)}", file=sys.stderr)
    for rule, n in prefilter_breakdown.most_common():
        print(f"    - {rule}: {n}", file=sys.stderr)

    if not llm_todo or prefilter_only:
        if prefilter_only and llm_todo:
            print(f"  --prefilter-only: skipping {len(llm_todo)} LLM-bound issues (will run later)", file=sys.stderr)
        if not prefilter_rows:
            return {"repo": repo, "raw": len(raw_issues), "delta": 0, "errors": 0}
        merged = existing + prefilter_rows
        write_predictions_local(repo, merged)
        manifest[repo] = {
            "count": len(merged),
            "delta": len(prefilter_rows),
            "last_classified_at": prefilter_ts,
            "model": "prefilter" if prefilter_only else model,
        }
        if push:
            try:
                push_predictions(api, repo_id, repo, merged, manifest)
                print(f"  [hf] pushed predictions/{repo.split('/', 1)[1]}.jsonl ({len(merged)} rows, +{len(prefilter_rows)} prefilter)", file=sys.stderr)
            except Exception as e:
                print(f"  [hf] push failed: {e}", file=sys.stderr)
                return {"repo": repo, "raw": len(raw_issues), "delta": len(prefilter_rows), "errors": 0, "push_error": str(e)}
        return {"repo": repo, "raw": len(raw_issues), "delta": len(prefilter_rows), "errors": 0}

    todo = llm_todo  # remainder goes to LLM (Groq or Anthropic)
    system_prompt = build_system_prompt()
    sem = asyncio.Semaphore(concurrency)
    new_rows: list[dict[str, Any]] = []
    errors = 0
    error_samples: list[str] = []
    counts: Counter[str] = Counter()
    completed = 0
    successes = 0
    last_checkpoint = 0
    t0 = time.monotonic()
    lock = asyncio.Lock()
    checkpoint_lock = asyncio.Lock()
    abort_event = asyncio.Event()

    # Fail-fast guardrail: if every one of the first FAIL_FAST_THRESHOLD
    # calls errors out (zero successes), abort the whole run instead of
    # grinding through hundreds of retries. Caught us once when a free-tier
    # quota was exhausted and the workflow burned 90 minutes on 429s.
    FAIL_FAST_THRESHOLD = 15

    # Checkpoint every CHECKPOINT_INTERVAL successful classifications. At
    # Tier-1's 3 RPM, no single repo can finish within a 90-min CI run, so
    # without checkpointing every timeout would discard hours of LLM work.
    # Pushes are idempotent on issue id — restart-safe.
    CHECKPOINT_INTERVAL = 50

    async def maybe_checkpoint() -> None:
        """Snapshot current state and push to HF. Serialized via checkpoint_lock."""
        if not push:
            return
        async with checkpoint_lock:
            async with lock:
                snapshot_rows = [r for r in new_rows if "error" not in r]
                snap_successes = successes
            if not snapshot_rows:
                return
            snapshot_merged = existing + prefilter_rows + snapshot_rows
            manifest[repo] = {
                "count": len(snapshot_merged),
                "delta": len(prefilter_rows) + len(snapshot_rows),
                "last_classified_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "model": model,
                "in_progress": True,
            }
            print(f"  [checkpoint] pushing {len(snapshot_merged)} rows "
                  f"({len(prefilter_rows)} prefilter + {len(snapshot_rows)} LLM, "
                  f"{snap_successes} LLM successes so far)", file=sys.stderr, flush=True)
            try:
                await asyncio.to_thread(push_predictions, api, repo_id, repo, snapshot_merged, manifest)
            except Exception as e:
                print(f"  [checkpoint] push failed (will retry next checkpoint): {e}", file=sys.stderr)

    async def task(issue: dict[str, Any]) -> None:
        nonlocal completed, errors, successes, last_checkpoint
        if abort_event.is_set():
            return
        async with sem:
            if abort_event.is_set():
                return
            pred = await classify_one(client, system_prompt, issue, model, backend)
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
                                f"  Check API keys and quotas (Groq: console.groq.com; Anthropic: console.anthropic.com).\n"
                                f"  Pipeline is idempotent — already-classified issues will be skipped.\n",
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
                if completed <= 5 or completed % 25 == 0 or completed == len(todo):
                    rate = completed / (time.monotonic() - t0)
                    eta = (len(todo) - completed) / rate if rate else 0
                    print(f"  [{completed}/{len(todo)}] rate={rate:.2f}/s eta={eta/60:.0f}min ok={successes} errors={errors}", file=sys.stderr, flush=True)
                should_checkpoint = (
                    successes > 0
                    and successes - last_checkpoint >= CHECKPOINT_INTERVAL
                )
                if should_checkpoint:
                    last_checkpoint = successes
            if should_checkpoint:
                await maybe_checkpoint()

    await asyncio.gather(*[task(i) for i in todo])
    if abort_event.is_set():
        print(f"  aborted after {completed} attempts, {errors} errors, {successes} successes", file=sys.stderr)

    llm_successful = [r for r in new_rows if "error" not in r]
    failed = [r for r in new_rows if "error" in r]
    successful = prefilter_rows + llm_successful  # both count as "delta this run"
    merged = existing + successful

    if failed:
        print(f"  {len(failed)} LLM failures dropped (will retry on next run)", file=sys.stderr)
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
            print(
                f"  [hf] pushed predictions/{repo.split('/', 1)[1]}.jsonl "
                f"({len(merged)} rows, +{len(prefilter_rows)} prefilter, +{len(llm_successful)} LLM)",
                file=sys.stderr,
            )
        except Exception as e:
            print(f"  [hf] push failed: {e}", file=sys.stderr)
            return {"repo": repo, "raw": len(raw_issues), "delta": len(successful), "errors": errors, "push_error": str(e)}

    return {"repo": repo, "raw": len(raw_issues), "delta": len(successful), "errors": errors}


async def run_corpus(
    model: str | None,
    concurrency: int,
    limit: int | None,
    max_issues: int | None,
    push: bool,
    only_repo: str | None,
    prefilter_only: bool = False,
) -> int:
    load_env()
    client, backend = get_llm_client()
    model_eff = model or default_model_for_backend(backend)
    concurrency = cap_concurrency_for_backend(backend, concurrency)
    print(f"[llm] backend={backend} model={model_eff} concurrency={concurrency}", file=sys.stderr)
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
            s = await classify_repo(
                api, repo_id, repo, client, backend, model_eff, concurrency, per_repo_limit, push, manifest, prefilter_only=prefilter_only
            )
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
# Validate mode (optional regression: frozen v0 queue + labels on disk)
# -----------------------------------------------------------------------------


def load_validation_set() -> list[tuple[dict[str, Any], dict[str, Any]]]:
    if not (V0_DIR / "queue.jsonl").exists() or not (V0_DIR / "tagged.jsonl").exists():
        print("ERROR: missing v0/queue.jsonl or v0/tagged.jsonl (only needed for validate / validate-prefilter).", file=sys.stderr)
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


def run_validate_prefilter() -> int:
    """Validate pre-filter against the frozen v0 regression set (queue + labels).

    Hard contract: zero false negatives on AF-tagged. If any AF-tagged issue
    matches a pre-filter rule, the rule is wrong and must be tightened.
    """
    pairs = load_validation_set()
    af_caught: list[tuple[dict[str, Any], str, str]] = []  # FALSE NEGATIVES
    n_caught: list[tuple[dict[str, Any], str, str]] = []   # true negatives
    af_missed: list[dict[str, Any]] = []
    n_missed: list[dict[str, Any]] = []

    for issue, gold in pairs:
        gold_norm = normalize_gold(gold)
        if gold_norm == "skip":
            continue
        hit = apply_prefilter(issue)
        is_af = gold_norm != "n"
        if hit and is_af:
            af_caught.append((issue, hit[0], hit[1]))
        elif hit and not is_af:
            n_caught.append((issue, hit[0], hit[1]))
        elif not hit and is_af:
            af_missed.append(issue)
        else:
            n_missed.append(issue)

    af_total = len(af_caught) + len(af_missed)
    n_total = len(n_caught) + len(n_missed)

    print(f"validation set: {af_total} AF-tagged + {n_total} not-a-failure = {af_total + n_total}\n")
    print(f"PREFILTER RECALL ON 'n':   {len(n_caught)}/{n_total} = {100 * len(n_caught) / max(n_total, 1):.1f}%")
    print(f"  → these would skip the LLM, saving API spend on obvious 'n' rows\n")

    if af_caught:
        print(f"FALSE NEGATIVES ({len(af_caught)}) — pre-filter wrongly dropped real AF issues:")
        for issue, rule, cat in af_caught:
            print(f"  ✗ {issue['id']} [{rule}]")
            print(f"      title: {issue['title'][:90]}")
        print("\nFIX one or more rules before shipping. Pre-filter is supposed to have 0 false negatives.")
        return 1
    print("FALSE NEGATIVES on AF-tagged: 0  ✓ (contract held)\n")

    if n_caught:
        print(f"TRUE NEGATIVES ({len(n_caught)}) — pre-filter correctly dropped these:")
        by_rule: dict[str, list[dict]] = {}
        for issue, rule, cat in n_caught:
            by_rule.setdefault(rule, []).append(issue)
        for rule, items in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
            print(f"  [{rule}] x{len(items)}")
            for issue in items[:2]:
                print(f"      {issue['title'][:90]}")
            if len(items) > 2:
                print(f"      (+{len(items) - 2} more)")
    return 0


async def run_validate(model: str | None, concurrency: int) -> int:
    load_env()
    client, backend = get_llm_client()
    model_eff = model or default_model_for_backend(backend)
    concurrency = cap_concurrency_for_backend(backend, concurrency)
    system_prompt = build_system_prompt()
    pairs = [(i, g) for i, g in load_validation_set() if normalize_gold(g) != "skip"]

    print(f"validation set: {len(pairs)} v0 regression pairs")
    print(f"backend: {backend}, model: {model_eff}, concurrency: {concurrency}\n")

    sem = asyncio.Semaphore(concurrency)

    async def task(issue, gold):
        async with sem:
            pred = await classify_one(client, system_prompt, issue, model_eff, backend)
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

    print(f"completed {total} classification calls (see provider dashboard for spend)")
    return 0


# -----------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    pp = sub.add_parser("validate-prefilter",
                         help="Run the deterministic pre-filter against v0 and report recall + false negatives.")

    pv = sub.add_parser("validate", help="Optional: compare LLM to frozen v0 regression labels (incidents/tagged/v0/).")
    pv.add_argument("--model", default=None, help="Override model (default: GROQ_MODEL or llama-3.1-8b-instant on Groq; ANTHROPIC_MODEL or claude-haiku-4-5 on Anthropic).")
    pv.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)

    pr = sub.add_parser("run", help="Classify any unclassified issues, push to HF.")
    pr.add_argument("--model", default=None, help="Override model id (same defaults as validate).")
    pr.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                    help=f"Concurrent LLM requests (default {DEFAULT_CONCURRENCY}; lower on 429s).")
    pr.add_argument("--limit", type=int, default=None, help="Cap classifications per repo (smoke test).")
    pr.add_argument("--max-issues", type=int, default=None,
                    help="Cap total classifications across all repos this run. Useful in CI to stay under job timeout.")
    pr.add_argument("--repo", default=None, help="Only classify this one repo (e.g. langchain-ai/langchain).")
    pr.add_argument("--no-push", action="store_true", help="Skip HF upload (local-only run).")
    pr.add_argument("--prefilter-only", action="store_true",
                    help="Run only the deterministic pre-filter; skip the LLM phase. "
                         "Use as a fast first pass before spending tokens.")

    args = parser.parse_args()
    if args.cmd == "validate-prefilter":
        return run_validate_prefilter()
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
            prefilter_only=args.prefilter_only,
        ))
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
