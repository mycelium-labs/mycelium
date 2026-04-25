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