# Mycelium Scope

## What is Mycelium

Mycelium is a runtime failure prevention layer for AI agents. It sits inside the agent loop and actively prevents failures before they reach the LLM.

**We don't monitor. We don't fix after the fact. We prevent.**

## Problem

AI agents fail in predictable, preventable ways. The LLM sees stale data, truncated outputs, broken transcripts — and confidently acts on them. The agent doesn't crash. It just gives wrong answers. Developers spend hours debugging what turned out to be a context problem.

## User Persona

Anyone building reliable AI agents — from solo developers shipping side projects to teams running agents in production.

## Evidence

507+ real-world agent failure incidents scraped from GitHub issues across major agent frameworks (LangChain, CrewAI, AutoGen, etc.). Context corruption (AF-006) is the #1 failure mode by human-tagged frequency.

## What We Are NOT Building

- **Not an observability platform.** Langfuse, Helicone, and Opik already do post-hoc tracing. We prevent, not observe.
- **Not a runtime monitor.** agentmw wraps clients and catches failures mid-flight. We prevent them from happening.
- **Not a fixer.** We don't repair broken agents. We make sure context is healthy before it reaches the LLM.
- **Not framework-specific.** We work with raw message lists. No LangChain, CrewAI, or AutoGen dependency.

## Failure Mode Taxonomy

| ID | Failure Mode | Description | Status |
|---|---|---|---|
| AF-001 | Hallucination cascade | Agent confidently acts on fabricated facts, compounding errors across tool calls | Future |
| AF-002 | Observability black hole | Consequential actions leave no trace — auditing/debugging impossible | Future |
| AF-003 | Infinite reasoning loops | Same reasoning cycle repeats; no progress, token burn | Future |
| AF-004 | Tool misuse | Tool calls with invalid inputs or outside intended scope; silent failure or wrong side effects | Future |
| AF-005 | Goal misalignment | Optimizes for a proxy objective, not user intent | Future |
| AF-006 | Context corruption | Stale, truncated, or poisoned context → false picture of the world | **v0** |
| AF-007 | Premature termination | Stops before done; presents partial state as final | Future |
| AF-008 | Cascading permission | Narrow permissions escalate transitively beyond intent | Future |
| AF-009 | Instruction injection | Untrusted content hijacks instructions | Future |

## v0 — Context Corruption Prevention

### Why AF-006 first

1. **Highest signal.** In our 507 human-tagged corpus, AF-006 dominates.
2. **Most actionable.** Stale data has a clear fix: track freshness, auto-refetch.
3. **Lowest integration friction.** One decorator per tool. No LLM call wrapping.
4. **Proven.** The archive branch shipped `@protect` + `Session` + `MessageValidator` and validated the approach.

### What v0 ships

| Component | What it does |
|---|---|
| `@protect` decorator | Wraps any tool function. TTL cache with per-entity keys. Auto-refetches when stale. |
| `protect_sync` | Same as `@protect` for synchronous tools (CrewAI, Smolagents). |
| `Session` | Per-run cache isolation. Prevents cross-request/context leakage via `ContextVar`. |
| `MessageValidator` | Catches broken transcripts before LLM call: orphan tool results, duplicate IDs, bad roles. `repair()` fixes what it can, raises on unfixable. |
| `HistoryGuard` | Validates message history: token overflow, silent drops, duplicate turns. |

### How it works

```python
from mycelium import protect, Session, MessageValidator, HistoryGuard

# Step 1: Decorate tools (one line per tool)
@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

# Step 2: Use Session per agent run
async with Session():
    customer = await fetch_customer(customer_id="c1")

# Step 3: Validate before LLM call
messages = MessageValidator().repair(messages)
HistoryGuard(max_tokens=100000).validate(messages)

# Step 4: Call LLM with clean context
response = await llm.ainvoke(messages)
```

### What v0 prevents

| Corruption type | How |
|---|---|
| Stale tool results | TTL-based cache. Auto-refetch when `age >= ttl`. |
| Cross-entity cache bleed | Entity ID in cache key. `fetch_user("alice")` and `fetch_user("bob")` are separate entries. |
| Cross-request leakage | `Session` + `ContextVar`. Each agent run gets its own cache. |
| Caching after errors | Exception clears cache entry. Next call refetches. |
| Broken transcripts | `MessageValidator` catches orphan tool results, duplicate IDs, bad roles. |
| Oversized history | `HistoryGuard` raises when token count or message count exceeds limits. |
| Silent message drops | `HistoryGuard` detects fingerprint gaps between turns. |

### What v0 does NOT prevent

| Corruption type | Why not | Future path |
|---|---|---|
| Truncated tool outputs | Transport/serialization layer, not cache layer | AF-004 (tool misuse) |
| Redundant context | Needs deduplication logic across messages | AF-006 v1 |
| Poisoned/injected context | Needs content security, not freshness | AF-009 (instruction injection) |
| Non-tool context (retrieval, user input) | `@protect` only wraps tools | Context registry (future) |
| Non-deterministic tools | Different values for same input can't be cached | `deterministic=False` flag (already in archive) |

## Post-v0 Roadmap

### v1 — Tool Boundary Enforcement (AF-004)

Tool misuse is the #1 failure mode by corpus frequency (575 occurrences). v1 adds:

- **Typed tool boundaries** — validate tool inputs against schema before calling
- **Scope enforcement** — tools declare what they're allowed to access, violations raise
- **Output validation** — check tool returns match expected shape

### v2 — Observability Hooks (AF-002)

Agents take consequential actions with no trace. v2 adds:

- **Audit trail** — every tool call, cache decision, and guard check logged
- **Action verification** — confirm side effects actually happened
- **Structured logging** — emit events that plug into existing observability stacks

### v3 — Loop Detection (AF-003)

Agents get stuck in reasoning loops, burning tokens. v3 adds:

- **Loop detector** — detect when the same reasoning pattern repeats N times
- **Circuit breaker** — force exit after configurable loop threshold
- **Progress tracker** — measure whether each step advances toward the goal

### v4+ — Remaining failure modes

- **AF-001** Hallucination cascade — cross-reference claims against source context
- **AF-005** Goal misalignment — track objective drift across steps
- **AF-007** Premature termination — verify completeness before stopping
- **AF-008** Cascading permission — enforce permission boundaries transitively
- **AF-009** Instruction injection — separate instructions from data in context

## Architecture Principles

### 1. Prevent, don't observe

We are not a dashboard. We are not a tracer. We are a guardrail. Every component either prevents a failure or it doesn't ship.

### 2. Framework-agnostic

No dependency on LangChain, CrewAI, AutoGen, or any agent framework. We work with raw message lists and plain Python functions.

### 3. Zero LLM calls (for v0)

No API key needed. No model calls for detection. Everything is deterministic, fast, and cheap. This is what makes us different from runtime monitors.

### 4. Opt-in per guard

Not every agent needs every guard. Import only what you need:

```python
from mycelium import protect, Session          # minimum
from mycelium import MessageValidator           # if you have tool calls
from mycelium import HistoryGuard               # if history grows large
```

### 5. Extensible failure mode registry

Each failure mode is a module. v0 ships AF-006. Future versions slot in AF-001 through AF-009 without changing the core API.

## Wedge

**Right now: only context corruption (AF-006).**

We start narrow and deep. Context corruption is the most common, most actionable, and most proven failure mode. We nail this first. Then we expand.
