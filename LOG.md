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