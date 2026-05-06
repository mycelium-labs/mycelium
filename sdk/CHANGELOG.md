# Changelog — mycelium-sdk

All notable changes to this package are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [0.1.0] — 2026-05-05

First public release.

### What this release is

`mycelium-sdk` protects AI agents from **AF-006 context corruption** — the class of failures where an agent reasons over stale, cross-contaminated, or error-poisoned tool results without knowing it. Version 0.1.0 ships the two primitives that form the complete public API.

### Public API

#### `@protect` — decorator for async tool functions

```python
from mycelium import protect

@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return await crm.get(customer_id)
```

Parameters:

| Parameter | Type | Default | Effect |
|-----------|------|---------|--------|
| `entity_param` | `str \| None` | `None` | kwarg name whose value scopes the cache. Different values get separate entries. Prevents cross-entity leakage (FM2). |
| `ttl` | `float` | `300` | Seconds before a cached result is stale. After expiry, the real function is called and the cache is refreshed. Prevents stale data (FM1). |
| `critical` | `bool` | `False` | If `True`, bypass the cache entirely on every call — no read, no write. For write-path tools and reads where staleness is never acceptable (FM4). |

The decorator is transparent to callers: a protected function has the same signature and return type as the original. Works in LangGraph, CrewAI, AutoGen, OpenAI Agents, Smolagents, or any async Python code without framework-specific changes.

`protect_sync` is the equivalent for synchronous tool functions (e.g. CrewAI tools, smolagents `Tool.forward`).

#### `Session` — per-run cache scope

```python
from mycelium import Session

async with Session() as s:
    c1 = await fetch_customer(customer_id="c1")   # cache_add
    c1 = await fetch_customer(customer_id="c1")   # cache_hit
    c2 = await fetch_customer(customer_id="c2")   # cache_add  (separate entry)

print(s.audit_log())   # list of cache events
print(s.cache_size())  # number of live (non-expired) entries
s.invalidate("fetch_customer", "c1")  # evict one entry immediately
```

`Session` uses a `ContextVar` so each `async with Session()` block has its own isolated cache. Concurrent agent runs on different async tasks never share state. Without an explicit `Session`, a module-level global session is used (fine for scripts; use explicit sessions in servers).

### Cache key

Cache key is `"{function_name}:{entity_id}"`. The function name prevents cross-source mixing (FM3); the entity ID prevents cross-entity leakage (FM2). Both components are always in the key.

### Audit events

Every cache operation appends to `session.audit_log()`:

| Event | When |
|-------|------|
| `cache_add` | First call for a key, or after stale/error refetch |
| `cache_hit` | Call within TTL window |
| `cache_stale` | Call after TTL expired — refetch triggered |
| `cache_error` | Wrapped function raised — entry cleared, exception re-raised |

### Failure modes covered

| # | Failure mode | Mechanism |
|---|-------------|-----------|
| FM1 | Stale data | `ttl` expires → `cache_stale` → real function called |
| FM2 | Cross-entity leakage | `entity_param` includes entity value in cache key |
| FM3 | Cross-source mixing | Function name included in cache key |
| FM4 | Behavioral drift | `critical=True` → cache never written or read |
| FM5 | Unbounded memory growth | `Session` scope + TTL expiry bounds live entries |
| FM6 | Concurrent confusion | `ContextVar` isolation + per-key writes |
| FM7 | Error invalidation | Exception clears the entry → next call gets fresh data |

### Validated against

- **507 real AF-006 failures** from `ndileep/mycelium-agent-failures` (HuggingFace dataset), across Cline, LiveKit Agents, AutoGen, OpenHands, LangChain, LangGraph, Smolagents, Stagehand, CrewAI, OpenAI Agents — 95.1% (482/507) mapped to protection mechanisms above.
- **180 tests** in [`agent-test-AF006`](https://github.com/mycelium-labs/agent-test-AF006): 47 direct integration (FM1–FM7), 22 property-style parametrized, 12 adversarial, 10 LiveKit #5408 real issue, 30 scenario reproducers, 70 framework integration.
- **Benchmark**: 190K–490K protected calls/sec in-process (see `BENCHMARK_ANALYSIS.md` in test repo).

### `StreamGuard` — streaming corruption protection

```python
from mycelium import StreamGuard, StreamCutOffError

async with StreamGuard(format="openai") as guard:
    async for chunk in llm.astream(prompt):
        chunk = guard.process(chunk)
        if chunk is not None:       # None = duplicate, skip it
            yield chunk
# raises StreamCutOffError if stream ended without a stop signal
```

Handles two streaming failure classes from the AF-006 dataset:

**Cut-off detection** — raises `StreamCutOffError` when the stream ends without a recognised stop signal. The agent never silently processes a partial response.

**Duplicate chunk detection** — hashes each chunk's text content; drops exact duplicates and mid-stream replays inline, emitting `stream_duplicate` audit events.

Parameters:

| Parameter | Type | Default | Effect |
|-----------|------|---------|--------|
| `format` | `"openai" \| "anthropic" \| None` | `None` | Built-in stop-signal adapter |
| `stop_validator` | `Callable[[chunk], bool] \| None` | `None` | Escape hatch for any other format |
| `deduplicate` | `bool` | `True` | Drop repeated chunks |

Built-in stop signals:
- **OpenAI**: `choices[0].finish_reason is not None`
- **Anthropic**: `type == "message_stop"` or `type == "message_delta"` with `stop_reason` set

Audit events: `stream_start`, `stream_chunk`, `stream_stop`, `stream_duplicate`, `stream_complete`, `stream_cutoff`. Written to `guard.audit_log()` and, if an active `Session` is present, to `session.audit_log()` as well.

### Not in this release

- Persistence across process restarts (cache is in-memory only)
- Distributed cache backend (Redis, Memcached)
- Automatic `entity_param` inference from function signature
- Streaming content caching / replay
- Protection classes other than AF-006 (loop detection, tool misuse, observability stubs exist in `mycelium.protections` but are not part of the stable public API yet)

### Internal API note

`mycelium.core.runtime_context_corruption` (`AgentRuntimeWithContextProtection`, `ContextCache`, `InvalidationPolicy`, etc.) is a step-based internal implementation used during development. It is not part of the public API, not covered by semver, and will be removed in a future release. All production code should use `@protect` + `Session`.

### Installation

```bash
pip install mycelium-sdk
# or from source:
pip install -e path/to/mycelium/sdk
```

Requires Python 3.12+.
