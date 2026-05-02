# Mycelium — Work Log

Short session notes. Newest on top. Keep entries tight: 3–6 lines each.

Template:

```
## YYYY-MM-DD — short title
- did: ...
- found: ...
- now: ...
- next: ...
```

---

## 2026-05-02 — Groq 429: parse minutes in retry hint

- did: `_parse_try_again_seconds()` parses **`9m15.3792s`**-style hints (TPD/TPM); Groq backoff sleep cap raised **180s → 1200s** so multi-minute waits are not truncated.

## 2026-05-02 — Groq TPM: pace starts + default cap 2

- did: `DEFAULT_CONCURRENCY` and `GROQ_MAX_CONCURRENCY` default **2**; **`pace_groq_request_start()`** (env **`GROQ_MIN_START_INTERVAL`**, default 0.5s) spaces request starts; 429 backoff min **2s**, sleep cap **180s**, **`max_retries` 12**; CI workflow default concurrency **2**.

## 2026-05-02 — Groq TPM: default concurrency 4 + cap

- did: `--concurrency` default **4**; Groq runs capped by **`GROQ_MAX_CONCURRENCY`** (default 4) to avoid free-tier TPM 429s; set **`GROQ_MAX_CONCURRENCY=16`** on paid tier if needed.
- did: longer backoff on Groq 429 (parse `try again in …ms`, min 1s); CI workflow default concurrency **4**.

## 2026-05-02 — `.env.example` for classify / HF secrets

- did: added `.env.example` (placeholders only); copy to `.env` locally — never commit real keys.

## 2026-05-02 — CI: scheduled HF runs force Groq

- did: `classify-issues.yml` exports `LLM_BACKEND=groq` for `schedule` and `workflow_run`; only `workflow_dispatch` with non-empty `llm_backend` can pick Anthropic.
- now: add `GROQ_API_KEY` to GitHub secrets for cron; your local `python scripts/classify_corpus.py run` keeps using whatever keys/`LLM_BACKEND` you have until you change them.

## 2026-05-02 — Groq Llama default for classify (Anthropic optional)

- did: classifier supports **Groq** (`openai` SDK, base `api.groq.com`) with default `llama-3.1-8b-instant`; **Anthropic** still works via `ANTHROPIC_API_KEY`. If both keys exist, **Groq wins** unless `LLM_BACKEND=anthropic`.
- did: workflow installs `openai` + `anthropic`; added secrets slot `GROQ_API_KEY`, optional dispatch `llm_backend`.
- now: set `GROQ_API_KEY` in `.env` / GH secrets; `uv pip install openai`.
- next: smoke `python scripts/classify_corpus.py run --no-push --limit 5` and confirm `[llm] backend=groq` in stderr.

## 2026-05-02 — HF predictions as sole catalog (LLM-only)

- did: documented in `classify_corpus.py` that the failure-mode catalog is only Hugging Face `predictions/` (prefilter + Claude); no separate human merge step.
- did: `build_review_pack.py` no longer dedupes against git `tagged.jsonl`; export is a read-only convenience; optional `ingest_proposed` / `tag_next` marked **legacy** in docstrings (v0 only for `validate` regression).
- now: `python scripts/classify_corpus.py run` is the one pipeline; `ingest_proposed` prints a one-line note that HF is canonical.
- next: run a full or capped `classify_corpus.py run` when ready; use `build_review_pack.py` only to skim AF exports.

## 2026-05-02 — classifier on Claude Haiku + CI restored

- did: replaced OpenAI with Anthropic `AsyncAnthropic` in `classify_corpus.py` (`messages.create`, JSON reply parsed from text). Default model `claude-haiku-4-5`, default concurrency 8.
- did: restored `.github/workflows/classify-issues.yml` with `ANTHROPIC_API_KEY`, `pip install anthropic`, scrape-trigger + 4h cron + dispatch inputs (`model`, `concurrency`, etc.).
- now: install deps with `uv pip install anthropic`; add `ANTHROPIC_API_KEY` to repo secrets; run `python scripts/classify_corpus.py run --no-push --limit 5` to smoke-test.
- next: `gh workflow run "Classify issues → HF predictions"` with small `limit` or full backfill when ready.

## 2026-04-26 (late) — phase 1 prefilter shipped, OpenAI dead-end on CI, switching to Anthropic

- did: built deterministic regex prefilter in `classify_corpus.py` (feature requests, docs, install errors, vendor pitches, how-to, typos, `[Roadmap]`, OS-specific). 11 rules, all title-only.
- did: shipped `validate-prefilter` subcommand with hard contract — zero false negatives on the 7 AF-tagged v0 issues. Currently catches 12/43 'n' (27.9%).
- did: ran prefilter-only across the full corpus in **47 seconds**. 1,244 issues classified deterministically and pushed to HF as `predictions/<repo>.jsonl` with `model: "prefilter:<rule>"` for full provenance.
- did: added HF checkpointing every 50 LLM successes (push during run, not just at end-of-repo) so a 90-min CI timeout doesn't discard hours of work.
- **dead end**: every OpenAI call from the GH Actions runner failed with `APIConnectionError: Connection error.` — across two CI runs, on two different keys, same result. Local Mac calls work fine. Likely IP-level block or org policy that doesn't surface as 401/429. **Not worth more debugging — moving to Anthropic.**
- removed: `.github/workflows/classify-issues.yml` (will be rewritten with Anthropic tomorrow). Existing pre-filter rows on HF are unaffected.
- next (tomorrow with Anthropic key): swap LLM client in `classify_corpus.py` → `anthropic` SDK with Claude Haiku, write fresh CI workflow, run full LLM phase on the ~14,570 remaining issues. Estimated wall time at Haiku rate limits: a few hours. Estimated cost: ~$5-10.

---

## 2026-04-26 — classification pipeline goes HF-native + auto-runs daily

- did: rewrote `classify_corpus.py` to read raw issues from HF and write predictions to `predictions/<repo>.jsonl` (append-only, idempotent by issue id). One file per repo on HF instead of a flat local jsonl.
- did: added `.github/workflows/classify-issues.yml` — fires on `workflow_run` after `scrape-issues.yml` succeeds, so freshly-scraped issues get classified within minutes. Manual `workflow_dispatch` for backfills with `--limit` / `--repo` knobs.
- did: updated `build_review_pack.py` to pull predictions from HF; updated the dataset card to document the new `predictions/` folder.
- now: HF dataset is the single source of truth in both directions (raw + predictions). Local-only smoke test classified 3 smolagents issues correctly, including one AF-007 hit. Pipeline committed but not run end-to-end yet — needs `OPENAI_API_KEY` as a GH secret + tier-2 unlock to do the 15.8k backfill.
- next: add GH secret + bump OpenAI to tier 2 → manually trigger the workflow once for the full backfill → review pack from `build_review_pack.py`.

---

## 2026-04-26 — v0 corpus complete, top-2 modes are AF-006 and AF-002

- did: hand-tagged 7 issues (1 AF-002, 6 not-a-failure). Got bored — most GitHub issues aren't agent failures. Switched to AI-proposed-human-reviewed for the remaining 43.
- did: sharpened AF-007 spec — silent false completion only; loud crashes are out of scope (Sentry territory). Disambiguator: "if the user can see it failed, it's not AF-007."
- did: shipped `scripts/ingest_proposed.py` + `incidents/tagged/v0/proposed.{md,jsonl}`. Claude classified 43 issues; 6 got AF tags, 37 not-a-failure.
- found: combined corpus distribution = AF-006 ×5, AF-002 ×2, AF-009 ×1. The top-2 v1 failure modes pick themselves: **context corruption + observability**. AF-001/003/004/005/008 had zero hits in this sample.
- found: best individual finds — crewAI #5057 (memory injection persisting across sessions, CVE-shaped), cline #7462 (mode confusion at >100k tokens, reproducible), stagehand #914 + langchain #36703 (agent-level LLM calls bypass observability — same pattern across two vendors).
- now: corpus has 8 AF-tagged + 42 explicitly-labeled negatives. Issue #3 closes after the user reviews proposed.md and runs the ingest.
- next: review proposed.md, ingest, close #3, decide v1 scope on #4 (probably AF-006 + AF-002 given the data).

## 2026-04-25 — tagging tool shipped, full corpus on HF (15.8k issues)

- did: full backfill green — 15,792 issues across 10 repos on HF (95 MB). langchain undercount (663 vs ~9.4k expected) noted as a follow-up but not blocking.
- did: built `scripts/tag_next.py` — `build-queue` samples 5/repo from HF (deterministic, seed=42), then interactive tag loop with required evidence + confidence. Resumable; appends to `incidents/tagged/v0/tagged.jsonl` (the moat, lives in git not HF).
- now: tooling done. Solo-tagging 50 issues is the unblocker for v1-scope decision (#4).
- next: run `build-queue`, hand-tag the 50 over the next few sessions, then pick the top-3 AF-* by frequency.

## 2026-04-25 — caught the clobber bug, fixed full-vs-incremental paths

- did: audited HF dataset — only 158 issues total (langchain has 9,441, we had 17). Found root cause: scheduled cron at 07:57 UTC overwrote the manual full-rescrape's `2026-04-25.jsonl` from 07:14 UTC, since both wrote to the same path.
- did: added `--full` flag → writes to `full-YYYY-MM-DD.jsonl` (separate file, can't be clobbered). Manifest now tracks `last_full_count` separately. Workflow uses `--full` when `full_rescrape=true`. Smoke-tested locally on stagehand → 349 issues pulled (vs 3 incremental).
- now: fix on `main`. Need one manual `workflow_dispatch` with `full_rescrape=true` to get the real historical corpus on HF.
- next: trigger full backfill, wait ~30-40 min, then hand-tagging (#3) finally has a real pool to sample from.

## 2026-04-25 — pipeline green end-to-end across 10 repos

- did: secret-fix worked. Re-triggered `workflow_dispatch` full-rescrape — all 10 repos (langchain, langgraph, autogen, crewAI, openai-agents-python, smolagents, OpenHands, cline, stagehand, livekit/agents) landed on HF in 11 commits, ~508 KB.
- now: research corpus operational. Issue #1 is 2/3 done; final tick auto-completes after cron observed for 3 days.
- next: hand-tagging (#3) is now the only thing that matters this week. Plus rotate PAT (#5, 5 min).

## 2026-04-25 — company plan in GitHub (milestones + 29 issues)

- did: created 8 labels, 5 milestones with real due dates (May 2, May 16, May 30, Jun 20, Jul 24), 29 issues scoped across them. Plan now lives in the repo, not in a doc.
- now: M1 due in 7 days. First blocker: fix `MYCELIUM_HF_REPO` secret → CI green → start hand-tagging 50 issues.
- next: rotate PAT (#5), fix secret + rerun workflow (#1), then hand-tag (#3) to unblock v1-scope decision (#4).

## 2026-04-24 — CI live, first full-rescrape hit HF validation bug (session 3)

- did: committed + pushed daily CI workflow + top-10 repos.txt. First `workflow_dispatch` full-rescrape scraped langchain (656 issues) on the runner but `HfApi.create_repo` 400'd on repo-name validation — almost certainly a trailing-newline in the `MYCELIUM_HF_REPO` secret.
- did: hardened scraper — `.strip()` all HF env vars, explicit `_validate_hf_repo_id` with clear error messages, pushed fix.
- now: fix pushed to `main`. Waiting on secret re-entry + workflow re-trigger to confirm green.
- next: once green, open HF dataset and spot-check all 10 repo folders landed. Then start hand-tagging 50 random issues against AF-001…AF-009.

## 2026-04-24 — top-10 repos + daily CI wired (session 2)

- did: expanded `scripts/repos.txt` to top-10 agent repos (langchain, langgraph, autogen, crewAI, openai-agents-python, smolagents, OpenHands, cline, stagehand, livekit/agents). Wrote `.github/workflows/scrape-issues.yml` — cron 07:00 UTC daily, `--since yesterday` incremental, plus manual-trigger with full-rescrape toggle.
- found: `huggingface-cli` deprecated → `hf`. HF dataset viewer is PRO-only for private repos (cosmetic, ignore).
- now: ingestion pipeline code-complete but not yet live. Workflow + repos.txt uncommitted on laptop.
- next: commit+push, add `HF_TOKEN` and `MYCELIUM_HF_REPO` as repo secrets on GitHub, trigger priming full-scrape via workflow_dispatch.

## 2026-04-24 — github-issues scraper live, HF corpus started

- did: wired scraper to push JSONL + manifest to a private HF dataset. Set up .venv with uv. First scrape of langchain-ai/langchain = 656 issues pushed to `ndileep/mycelium-agent-failures` (5.49 MB, private).
- found: `huggingface-cli` renamed to `hf`; HF dataset viewer is PRO-only for private repos (ignore, irrelevant).
- now: ingestion pipeline works end-to-end for one repo. HF = source of truth, local buffer gitignored.
- next: expand `scripts/repos.txt` (langgraph, crewAI, autogen, openai-agents-python), run `--all`, then hand-tag 50 random issues against AF-*.

## 2026-04-23 — repo scaffolded, name locked

- did: set up git repo, scaffolded folder structure (sdk, benchmarks, examples, docs, incidents, research, scripts).
- did: went through full founding conversation; locked name = Mycelium, positioning = runtime substrate for agent failure modes.
- found: Garry Tan's Skillify/GBrain is philosophically aligned but different layer (personal workflow vs enterprise runtime). Not competitive.
- now: empty scaffold, no code yet. Dogfooding on own agents is the next real phase.
- next: run own agents for a week, start the incident log, pick the 3 failure modes for v1 based on real data.

new things for me - ruff, pyright