# SDK v1 scope — failure modes to protect first

**Decision date:** 2026-05-03  
**Inputs:** (1) full Hugging Face classifier corpus `predictions/*.jsonl`, (2) v0 hand-tag sample (`research/tag-frequency-v0.md`, `incidents/tagged/v0/`).

---

## 1. Full corpus — AF-* frequency (automated classifier)

Source: `scripts/analyze_af_frequency.py` over **`ndileep/mycelium-agent-failures`** — all 10 repos in `scripts/classify_corpus.py` `REPOS`.

| Metric | Value |
|--------|------:|
| Total prediction rows | 15988 |
| Rows with ≥1 AF tag | 2257 |
| Rows tagged `n` / no AF | 13731 |
| Rows with `error` | 0 |
| Prefilter-only rows (deterministic `n`) | 1257 |

**Per-mode counts** (compound labels count **each** mode once; e.g. `AF-006+AF-009` adds one to AF-006 and one to AF-009):

| Rank | AF-ID | Theme | Occurrences |
|-----:|-------|--------|------------:|
| 1 | **AF-004** | Tool misuse / boundary violations | **575** |
| 2 | **AF-006** | Context corruption | **501** |
| 3 | **AF-007** | Premature termination | **415** |
| 4 | AF-002 | Observability black hole | 304 |
| 5 | AF-003 | Infinite reasoning loops | 218 |
| 6 | AF-005 | Goal misalignment | 177 |
| 7 | AF-009 | Instruction injection | 22 |
| 8 | AF-001 | Hallucination cascade | 36 |
| 9 | AF-008 | Cascading permission | 9 |

**Caveat:** These are **LLM triage** labels (Groq/Anthropic over time), not human gold. They skew toward whatever the prompt emphasizes and can over-tag modes that read like generic “bugs.” Treat **rank order** as directional, not ground truth.

---

## 2. v0 human sample (50 issues)

From `tagged.jsonl` — AF assignments only (excluding pure `not-a-failure`):

| AF-ID | Human tag assignments |
|-------|------------------------:|
| AF-006 | 5 |
| AF-002 | 2 |
| AF-009 | 1 |

(8 AF label slots total across multi-labeled issues.)  
**Story:** In a curated random draw, **AF-006** dominates; **AF-002** is the clearest second signal. **AF-009** appears rarely but is security-critical when it does.

---

## 3. Decision — top 3 for SDK v1

| Priority | Mode | Rationale |
|----------|------|-----------|
| **1** | **AF-006** — Context corruption | #1 in human v0; #2 in full corpus. Maps to memory/context hygiene, invalidation, provenance — core “substrate” story. |
| **2** | **AF-004** — Tool misuse / boundary violations | #1 in corpus frequency; strongest match to **typed syscalls + capability gates** (enforceable at tool boundary). Aligns with enterprise “wrong tool / wrong args” incidents. |
| **3** | **AF-002** — Observability black hole | Strong in v0 (#2 human); moderate in corpus (304). Maps to **mandatory verification / traceability** after writes — CISO-legible. |

**Why not AF-007 first** despite high classifier count (415)? It overlaps linguistically with “agent stopped early” generic issues; we’ll fold **exit / completion checks** into the same runtime as AF-006/AF-004 where they collide, but **AF-007 is not a standalone v1 pillar** until we separate signal from noise in a human audit pass.

**Why not AF-009 in the top 3 despite injection headlines?** Low prevalence in both slices (22 LLM, 1 human). Still **on the near roadmap** for a security SKU — not the first three enforcement modules.

---

## 4. Next actions

- Implement protections in dependency order: **tool boundary + capabilities (AF-004)** → **context / memory integrity (AF-006)** → **trace + verify after writes (AF-002)**.
- Revisit ranks after **human spot-check** of 100 random **AF-004** / **AF-007** labels (estimate classifier precision).
