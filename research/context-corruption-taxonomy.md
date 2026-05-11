# Context corruption taxonomy (working list)

**Purpose:** Enumerate *classes* of real-world **context corruption** — situations where the state an agent (or its orchestrator) reasons over diverges from ground truth without the stack surfacing a clear error.

**Not exhaustive:** New frameworks, modalities, and deployment patterns add new classes. This list is a **living checklist** for product and research, not a closed formal spec.

**Canonical AF-006 row** (high-level): see `research/failure_modes.md` — *Stale, truncated, or poisoned context → false picture of the world.*

---

## Legend — Mycelium SDK (public API as of `sdk/mycelium/__init__.py`)

| Symbol | Meaning |
|--------|---------|
| ✅ | **Guardrail shipped:** `protect` / `protect_sync`, `Session`, `StreamGuard`, `HistoryGuard`, `MessageValidator`, or `ContentBlockNormalizer` **directly targets** this class when used at the right boundary. |
| ⚠ | **Partial:** Some mitigation, opt-in behavior, or correct integration required; does **not** fully eliminate the class. |
|  | **Not implemented** in the current public SDK for this class. |

**Primitives referenced:** `@protect` / `protect_sync` + `Session` (TTL cache, per-tool and per-entity keys, `critical=True` bypass, error invalidation, audit); `StreamGuard`; `HistoryGuard`; `MessageValidator` / `repair()`; `ContentBlockNormalizer`; `AsyncClient` / `Client` (HTTP transport completeness).

Internal stubs (`mycelium.protections.*` loop/tool misuse/observability) are **out of scope** for this column unless they become stable public API.

---

## A. Tool and environment boundary

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Stale tool results (backend moved on; cached answer old) | ✅ | `@protect(ttl=…)` + `cache_stale` path; `critical=True` when staleness is never OK. |
| Wrong tool chosen / invalid arguments (valid-looking garbage) |  | Tool-boundary enforcement is **AF-004** territory; not a stable public guard here. |
| Partial tool payloads (timeout, truncated HTTP/stream JSON) | ✅ | `AsyncClient` / `Client` check Content-Length mismatch, JSON structural truncation, and empty JSON bodies. `PayloadIncompleteError` flows through `@protect` error invalidation. |
| Lossy or ambiguous serialization (dates, IDs, enums) across layers |  | Application responsibility. |
| Non-deterministic tools (same call, different truth) | ⚠ | TTL forces **refetch**; does not make tools deterministic or resolve split-brain. |
| Read-replica lag (“shadow reads”) | ⚠ | Fresh read after TTL may hit a different replica timing; no quorum / version token. |
| Wrong tenancy / region / shard (shape OK, wrong customer) | ✅ | `@protect(entity_param=…, entity_field=…)` validates round-trip: response field must match request entity. `TenancyMismatchError` raises on DB-routing or proxy bugs; cache cleared, agent can retry. |
| Poisoned or hostile tool content (untrusted page as “data”) |  | No content-security / sandbox for tool outputs; overlaps **AF-009**. |

---

## B. Caching, memoization, and reuse

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Cross-entity cache collision | ✅ | `entity_param` + entity value in cache key. |
| Cross-tool / cross-function cache collision | ✅ | Function name in cache key. |
| Cross-session or cross-request leakage | ✅ | `Session` + `ContextVar`; explicit `async with Session()` per run. |
| Unbounded memory growth of cache | ⚠ | TTL expiry + session scope **bound live entries**; no hard max-entry cap. |
| “Negative caching” (cache 404 / empty; state later exists) |  | No special negative-cache policy. |
| Errors or exceptions cached as success | ✅ | Exception clears cache entry (`cache_error`); next call refetches. |
| Write-through / ordering mismatch (local vs durable) |  | No distributed transaction or version-vector guard. |

---

## C. Conversation history and planner-visible state

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Token-window overflow (history too large) | ✅ | `HistoryGuard.validate()` — `HistoryTruncatedError` when over `max_tokens` / `max_messages`. |
| Silent **drop** of messages between turns | ✅ | `HistoryGuard.check_for_drops()` — fingerprint monotonicity. |
| Silent **summarization / compaction** (facts replaced by lossy summary) | ⚠ | Drop detection sees **count** changes, not semantic fidelity of summaries. |
| Duplicate or replayed turns in history |  | Not a dedicated detector. |
| Role / channel mis-tagging (system vs user vs tool) | ✅ | `MessageValidator` — `invalid_role`. |
| Wrong checkpoint / resume token (time-travel) |  | Out of scope. |
| Parallel tool calls merged incorrectly into linear transcript | ⚠ | `MessageValidator` helps **indices / ids / duplicates**; does not fix all merge strategies. |

---

## D. Tool-call and message-shape integrity

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Orphaned tool results (result without matching call) | ⚠ | Validator catches **missing `tool_call_id`** and structural issues; not every orphan pattern. |
| Mismatched / duplicate tool call IDs | ✅ | `MessageValidator` + `repair()`. |
| Duplicate tool call blocks (streaming partial + final) | ✅ | `repair()` drops `fc_*` partials, etc. |
| Provider / SDK message schema drift | ⚠ | `ContentBlockNormalizer` targets **known** provider block mismatches; not all versions. |
| Structured-output `parsed` artifacts left in history | ✅ | `repair()` strips `parsed_artifact`. |

---

## E. Streaming and partial model output

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Truncated completion (no stop / partial treated as final) | ✅ | `StreamGuard` — `StreamCutOffError` when stop signal missing. |
| Duplicate or replayed stream chunks | ✅ | `StreamGuard(deduplicate=True)` — duplicate hash drop + audit. |
| Out-of-order chunks from intermediaries |  | Not handled. |
| Incorrectly multiplexed sub-agent streams |  | Out of scope. |

---

## F. Retrieval and knowledge (RAG-shaped)

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Stale vector index vs source documents |  | No RAG / index TTL integration. |
| Bad chunking / missing spans in retrieved context |  | No retriever. |
| Wrong attribution (text vs source metadata) |  | No citation validator. |
| Poisoned or SEO corpus |  | No corpus trust model. |
| Retrieval conditioned on hallucinated sub-query |  | No HyDE / query guard. |
| Permission leakage via retrieval |  | Security / authz; not in SDK. |

---

## G. Multi-agent, subgraphs, and workflows

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Shared scratchpad without isolation | ⚠ | **Per-run** `Session` isolates **Mycelium cache**; does not isolate arbitrary shared memory between agents. |
| Subgraph state loss / bad merge (orchestrator graph) |  | Framework state machine; out of scope. |
| Handoff summary drops constraints / budgets |  | No handoff contract validator. |
| Map-reduce / fan-out merge bugs |  | Out of scope. |

---

## H. Time, concurrency, and ordering

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Interleaved tool races (ordering vs causality) | ⚠ | Per-key cache writes reduce **cache** races; does not order tool side effects. |
| Clock skew affecting TTL / OAuth / leases |  | Uses `time.monotonic()` for TTL; no wall-clock sync story. |
| Optimistic concurrency ignored on writes |  | No ETag / version enforcement. |

---

## I. Modalities beyond text

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| ASR / OCR errors in context |  | No modality pipeline. |
| Vision / UI misread (wrong element in reasoning) |  | No vision guard. |
| CSV / encoding / BOM confusion in files |  | No file ingest guard. |

---

## J. Infrastructure and deployment

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Split-brain / partial outage (half of deps stale) | ⚠ | TTL + refetch may **eventually** see newer data; no health-partition logic. |
| Feature flags / canary (inconsistent code paths) |  | Out of scope. |
| Version skew across replicas |  | Out of scope. |

---

## K. Human and process

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Wrong environment (prod vs staging in config) |  | Operator process. |
| Pasted secrets / PII become “facts” in context |  | No redaction layer. |
| Manual transcript edits introduce inconsistency |  | Out of scope. |

---

## L. Security-adjacent (often AF-009; co-occurs with AF-006)

| Class | Mycelium | Notes |
|-------|:--------:|-------|
| Indirect prompt injection in retrieved or user content |  | No instruction/data separation. |
| Malicious content returned as tool “data” |  | No sandboxed parsing. |
| Supply-chain compromise of prompts/tools |  | Out of scope. |

---

## Summary counts (rough)

- **✅ Direct coverage:** tool-result staleness and cache key classes; transport-level payload completeness (Content-Length, JSON truncation, empty body); stream cut-off/duplicate; history size and silent drops; several message/tool-call shape bugs; provider content-block normalization for documented cases.
- **⚠ Partial:** replica lag, entity scoping only as good as your ids, summary fidelity, some orphan patterns, multi-agent shared state beyond Mycelium cache, ordering of side effects, outage split-brain.
- **Gaps:** RAG, full multi-agent orchestration, modalities, infra canaries, injection, hard cache caps, negative caching.

**Related repo docs:** `research/failure_modes.md`, `research/v1-scope.md`, `sdk/README.md`, `sdk/CHANGELOG.md`, `sdk/PROOF_SUMMARY.md`.
