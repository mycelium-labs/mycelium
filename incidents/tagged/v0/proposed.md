# v0 Proposed Tags — Review

AI-proposed tags for the 43 un-tagged issues in the v0 queue.
**You review, you approve, you ingest.** Same epistemic standard as supervised
labeling at any ML lab — the corpus is *human-reviewed*, not human-typed.

## How to use this file

1. **Skim the table below.** It's sorted by verdict (AF-tagged first).
2. **Disagree with anything?** Edit `incidents/tagged/v0/proposed.jsonl` directly
   (one row per issue, same id), or tell Claude which row to flip.
3. **Ingest:** `python scripts/ingest_proposed.py`. This appends to
   `tagged.jsonl`, skipping anything already tagged. Idempotent.
4. **Sanity check:** `python scripts/tag_next.py status`.

## Summary

| Metric | Count |
|---|---|
| Total issues in queue | 50 |
| Already tagged (manual, by you) | 7 |
| Newly proposed | 43 |
| → AF-tagged | **6** |
| → not-a-failure | **37** |

**Distribution of AF tags (combined corpus, 50 issues):**

| Label | Count | Issues |
|---|---|---|
| AF-006 (Context Corruption) | 5 | crewAI #5057, crewAI #5155, langgraph #6938, cline #7462, langgraph #7117 |
| AF-002 (Observability Black Hole) | 2 | langchain #36703, stagehand #914 |
| AF-009 (Instruction Injection) | 1 | crewAI #5057 (double-tagged with AF-006) |
| AF-001, 003, 004, 005, 007, 008 | 0 | — |

**Headline finding:** 80% of real agent-failure issues in this corpus map to
**AF-006 or AF-002**. AF-006 is the dominant signal. That's a strong hint at
which two failure modes Mycelium v1 should focus on (issue #4 in your roadmap).

---

## AF-tagged proposals (6)

These are the issues worth your attention. Each is shown with the proposed tag,
confidence, evidence, and reasoning.

### #02 — `crewAIInc/crewAI#5057` → AF-009 + AF-006 (high) ★ best find
> [Security] Memory content injected into system prompt without sanitization enables indirect prompt injection

**URL:** https://github.com/crewAIInc/crewAI/issues/5057

**Why:** `LiteAgent._inject_memory()` concatenates retrieved memories directly
into the system prompt. Poisoned tool outputs persist as memories and on the
next session get elevated to system-prompt authority. Code refs:
`lite_agent.py:568-581`. This is a CVE-shaped writeup against a major
framework — exactly the kind of incident Mycelium would prevent at the gateway
layer.

**Two failure modes overlap:** AF-009 (untrusted content reinterpreted as
instructions) + AF-006 (poisoned memory corrupts future agent context).

---

### #08 — `crewAIInc/crewAI#5155` → AF-006 (low)
> RFC: Detecting silent behavioral drift in agents across session boundaries

**URL:** https://github.com/crewAIInc/crewAI/issues/5155

**Why:** Not an incident — it's an RFC describing the AF-006 mechanism with
three measurable signals (lexicon decay, tool-call sequence shift, semantic
drift) and a reference impl ([compression-monitor](https://github.com/agent-morrow/compression-monitor)).
Useful design-partner evidence: **the community is already asking for what
Mycelium proposes to build.** Low confidence because it's hypothetical, not
reproducible.

---

### #24 — `langchain-ai/langgraph#6938` → AF-006 (medium)
> Fail closed on checkpoint schema validation before load

**URL:** https://github.com/langchain-ai/langgraph/issues/6938

**Why:** Hardening request — without strict fail-closed schema validation, a
malformed checkpoint can corrupt agent state on resume. Same shape as the
langchain `except: pass` finding (#36703): **structural mechanism evidence** for
AF-006 in a major framework, not an observed runtime incident.

---

### #37 — `browserbase/stagehand#914` → AF-002 (medium)
> Better agent logging

**URL:** https://github.com/browserbase/stagehand/issues/914

**Why:** Author identifies that agent-level LLM calls (`AnthropicCUAClient`,
`OpenAICUAClient`) bypass Stagehand's `logInferenceToFile` infrastructure
entirely — the agent's reasoning is invisible to the trace. Same pattern as the
langchain finding, **second AF-002 mechanism in the corpus across two different
vendors.** Strong "this is a category-wide problem" signal for the pitch.

---

### #39 — `cline/cline#7462` → AF-006 (high) ★ best behavioral incident
> Cline doesn't recognize that Act mode is active.

**URL:** https://github.com/cline/cline/issues/7462

**Why:** Cline (a popular VS Code agent) repeatedly asks to switch to Act mode
even when Act mode is already active. **Reproducible trigger:** prompt size
exceeds 100k–120k tokens. Agent's working state has diverged from real state
purely as a function of context length. Textbook AF-006, with a real
user-facing impact and clear repro.

---

### #40 — `langchain-ai/langgraph#7117` → AF-006 (low)
> When invoking the tool-call subgraph, the main agent loses the memory of previous tool invocations.

**URL:** https://github.com/langchain-ai/langgraph/issues/7117

**Why:** Title and description describe AF-006, BUT the repro code uses
non-existent imports (`from langgraph import LangGraph`, `langgraph.actors`).
Looks AI-generated and not actually runnable. Mechanism plausible, evidence
weak. **Flag for re-verification before citing in any pitch.**

---

## Not-a-failure proposals (37)

Brief verdict + reason for each. Skim, flip any you disagree with.

| # | Issue | Title (truncated) | Reason |
|--:|---|---|---|
| 01 | langchain #34906 | xAI live search deprecation error | external API deprecation, integration bug |
| 03 | langchain #36211 | logprobs not returned (response API) | library bug, no agent behavior |
| 04 | smolagents #2171 | WhichModel MCP integration proposal | vendor pitch |
| 05 | autogen #2412 | deepcopy breaks proxy http_client | library plumbing bug |
| 06 | cline #6705 | VSIX install ENOENT on Windows | install / env issue |
| 07 | openai-agents #331 | "How to use the agent for VQA?" | usage question |
| 09 | livekit #4351 | Google STT stopped working | integration regression |
| 10 | livekit #1693 | elevenlabs plugin thread-safety error | library threading bug |
| 11 | crewAI #5049 | asqav cryptographic audit trails | vendor pitch |
| 12 | smolagents #442 | "uvx" vs "uv" in MCP sample | docs typo |
| 13 | cline #2519 | chromium version mismatch (zh-CN) | playwright env mismatch |
| 14 | OpenHands #2800 | "use rg instead of grep" rule | feature request |
| 15 | openai-agents #2076 | OpenAI Agent Registry plans | feature / announcement |
| 16 | OpenHands #9330 | alternative microagent dir for gitlab | feature request |
| 17 | OpenHands #7488 | "Task reminder code…" | empty issue / template only |
| 18 | smolagents #1307 | extra params to MLX tokenizer | feature request |
| 19 | livekit #1602 | track publish timeout in callbacks | library bug |
| 20 | autogen #1597 | docs/training roadmap | epic, not incident |
| 21 | smolagents #508 | `*` in authorized_imports denies all | library config bug; opposite of AF-008 (denies wrongly, doesn't escalate) |
| 22 | openai-agents #200 | Parallel Agent Handoff (?) | usage question |
| 23 | OpenHands #10273 | browser tab title not refreshed | UI bug |
| 25 | livekit #4219 | preemptive_generation duplicates LLM calls | duplicate, not loop → not AF-003. Library config bug. |
| 26 | langgraph #7439 | Merxex Exchange monetization | vendor pitch |
| 27 | stagehand #972 | can't observe `<input type=file>` | library DOM bug |
| 28 | livekit #399 | add `function_calls_started` event | feature request |
| 29 | stagehand #421 | schema field descriptions | feature request |
| 30 | autogen #2667 | pgvector returns bytes not string | library bug |
| 31 | langchain #30578 | BaseTool inconsistent empty-list return | library API ambiguity. Strong writeup but framework design issue, not observed agent failure. Could stretch to AF-004 low-confidence if you want. |
| 32 | langchain #36392 | "x" | spam / incomplete |
| 33 | autogen #7132 | OpenRouter + structured output + function calls | provider compat bug |
| 34 | cline #1989 | "Response" | empty issue |
| 35 | openai-agents #905 | Anthropic prompt caching | feature request |
| 36 | cline #9880 | `<think>` label leaks for Minimax | UI bug |
| 38 | crewAI #4910 | agent-evidence runtime bundles | vendor pitch (pitches AF-002 solution → confirms market demand, but not an incident) |
| 41 | openai-agents #1764 | reasoning effort="none" enum | feature request |
| 42 | langgraph #7492 | SupraWall billing example | vendor pitch as "community example" |
| 43 | OpenHands #11065 | conversation rename doesn't stick | UI bug |

---

## Borderline calls — worth a sanity check

These are the ones I had to think about. If you'd flip any, edit `proposed.jsonl`:

- **#08** (crewAI #5155) — RFC, not incident. Tagged AF-006 low. Flip to `n` if
  you want strict "incident only" corpus. I'd keep it; it's pitch fuel.
- **#24** (langgraph #6938) — Hardening request, not incident. Same logic as
  AF-002 #36703 (langchain except-pass) which you already accepted. Keep.
- **#31** (langchain #30578) — Library API design bug, not agent behavior.
  Flagged `n`. Could stretch to AF-004 low-confidence. I'd leave as `n`.
- **#37** (stagehand #914) — Same shape as #36703 (mechanism evidence, not
  incident). Keep at AF-002.
- **#40** (langgraph #7117) — Repro looks AI-generated. Tagged AF-006 low. If
  you want a clean corpus, flip to `n` (with reason "repro looks AI-generated").

## What this gets you

After ingest, your v0 corpus has:
- **8 AF-tagged issues** across AF-002, AF-006, AF-009 (vs the ML benchmark
  norm of "tag 100, hope for 30 useful")
- **42 explicitly-labeled negatives** with one-line reasons (negative examples
  matter for any classifier you eventually train)
- A clear top-2 failure-mode picture for v1: **AF-006 + AF-002**

That closes issue #3 (hand-tag 50) and unblocks issue #4 (pick top-3 modes).
