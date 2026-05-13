# Mycelium SDK

Runtime protection for AI agents against context corruption (AF-006).

Decorate your tools once. Use them normally in any framework. Mycelium handles the rest.

## Install

```bash
pip install ./sdk
```

## Usage

```python
from mycelium import protect, Session

@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

@protect(entity_param="sku", ttl=300)
async def get_inventory(sku: str) -> dict:
    return await warehouse.get(sku)
```

That's it. Call your tools normally in any framework — Mycelium intercepts at the function level.

```python
# LangGraph — no changes to how you use the graph
tool_node = ToolNode([fetch_customer, get_inventory])

# AutoGen — no changes to agent setup
agent = AssistantAgent(tools=[fetch_customer, get_inventory])

# Pydantic AI — register as a tool_plain
@agent.tool_plain
async def get_customer(customer_id: str) -> str:
    return str(await fetch_customer(customer_id=customer_id))

# Direct call — works the same everywhere
result = await fetch_customer(customer_id="c1")
```

## Framework support

Confirmed end-to-end with real framework invocation paths — no mocks:

| Framework | Invocation path | Sync/Async |
|---|---|---|
| LangGraph | `StateGraph.compile().ainvoke()` | async |
| AutoGen | `FunctionCall` → executor | async |
| CrewAI | `BaseTool._run(**calling.arguments)` | sync |
| Smolagents | `Tool.forward()` | sync |
| OpenAI Agents | `FunctionTool.on_invoke_tool()` | async |
| LiveKit Agents | `execute_function_call()` | async |
| LangChain | `tool.ainvoke(args_dict)` | async |
| Pydantic AI | `FunctionSchema.call()` → `await function(**kwargs)` | async |

Sync frameworks (CrewAI, Smolagents) use `protect_sync` — see below. All other frameworks use `protect`.

DSPy (`dspy.ReAct` modules) and Haystack (`@component` tools) follow the same async function call pattern and should work with `@protect` without changes — tests not yet written.

## Session isolation

Wrap each agent run in a `Session` to prevent cache leakage between runs:

```python
async with Session() as session:
    result = await fetch_customer(customer_id="c1")
    result2 = await fetch_customer(customer_id="c1")  # cache hit — no second DB call

# New run gets a clean cache
async with Session() as session:
    result = await fetch_customer(customer_id="c1")  # fresh call
```

Without an explicit `Session`, a global session is used — fine for single-agent scripts, not for production services handling concurrent requests.

## Write-after-read grace (replica-lag guard)

When your agent writes to a database and then immediately reads the same entity, the read may hit a lagging read replica and return stale data. Use `mark_as_write` on write tools and `read_after_write_grace` on read tools to force fresh reads for a short window after any write:

```python
@protect(entity_param="customer_id", mark_as_write=True)
async def update_balance(customer_id: str, amount: float) -> dict:
    return await db.update_balance(customer_id, amount)

@protect(entity_param="customer_id", ttl=60, read_after_write_grace=2.0)
async def get_balance(customer_id: str) -> dict:
    return await db.get_balance(customer_id)
```

```python
async with Session() as s:
    await update_balance(customer_id="c1", amount=50)  # write tracked
    balance = await get_balance(customer_id="c1")      # bypasses cache, fetches fresh
    # ... 3 seconds later ...
    balance2 = await get_balance(customer_id="c1")     # cache hit (grace expired)
```

- Write tracking is **per-entity** — a write to `c1` does not affect reads for `c2`
- Grace bypass results are still cached so reads after expiry can hit normally
- Works with `critical=True` write tools as well

## Parameters

### `@protect`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `entity_param` | `str \| None` | `None` | Kwarg name that identifies the entity. Different entity values get separate cache entries. |
| `ttl` | `float` | `300` | Seconds before a cached result is considered stale and the real function is called again. |
| `critical` | `bool` | `False` | Skip caching entirely — always call the real function. Use for tools where staleness is never acceptable. |
| `mark_as_write` | `bool` | `False` | Record this call as a write operation. Enables `read_after_write_grace` bypass for subsequent reads. |
| `read_after_write_grace` | `float` | `0.0` | Seconds after a write during which reads for the same entity bypass the cache. Use `2.0` to guard against read-replica lag. |
| `entity_pattern` | `str \| None` | `None` | Regex pattern the entity_id must match. Raises `EntityPatternError` on mismatch. E.g. `r"^c\d+$"` for customer IDs. |
| `cache_empty` | `float \| None` | `None` | Special TTL for empty results (`[]`, `{}`, `None`, `""`). `0` = never cache empties. |
| `deterministic` | `bool` | `True` | If `False`, skip caching (for non-deterministic tools like stock prices). |
| `max_entries` | `int \| None` | `None` | Hard cache cap on `Session`. LRU eviction when exceeded. |

### `Session`

| Method | Description |
|--------|-------------|
| `session.cache_size()` | Number of live (non-expired) entries |
| `session.audit_log()` | Full list of cache events (`cache_add`, `cache_hit`, `cache_stale`, `cache_error`) |
| `session.invalidate(tool_name, entity_id)` | Manually evict a specific entry |

## What it protects against

All AF-006 (context corruption) manifestations:

| Layer | Manifestation | How Mycelium prevents it |
|-------|--------------|-------------------------|
| **Tool cache** | Stale data | TTL expiry — refetches after `ttl` seconds |
| | Cross-entity leakage | Separate cache entry per `entity_param` value |
| | Cross-source mixing | Separate cache entry per function name |
| | Read-replica lag | `mark_as_write` + `read_after_write_grace` — forces fresh reads after writes |
| | Non-deterministic tools | `deterministic=False` skips caching |
| | Negative caching | `cache_empty` controls caching of empty results |
| | Error invalidation | Exceptions clear the cache entry automatically |
| | Unbounded growth | `Session(max_entries=N)` with LRU eviction |
| | Entity validation | `entity_pattern` regex validation raises `EntityPatternError` |
| | Tenancy mismatch | `entity_field` validates response matches request entity |
| **Streaming** | Cut-off stream | `StreamGuard` raises `StreamCutOffError` if stop signal missing |
| | Duplicate chunks | Content-hash deduplication |
| | Out-of-order chunks | `sequence_field` validates monotonic ordering |
| **Conversation history** | Token overflow | `HistoryGuard.validate()` raises before LLM call |
| | Silent drops | `HistoryGuard.check_for_drops()` fingerprint comparison |
| | Duplicate turns | `detect_duplicates=True` catches repeated messages |
| | Summary keyword loss | `track_keywords` + `check_summary_fidelity()` |
| | Excessive compaction | `max_compaction_ratio` detects aggressive summarization |
| **Message structure** | Orphaned tool results | `MessageValidator` catches missing/unmatched `tool_call_id` |
| | Misplaced tool results | Detects tool results appearing after subsequent assistant messages |
| | Duplicate tool-call blocks | `repair()` drops `fc_*` partials |
| | Non-zero tool-call indices | Auto-repairs to 0-based |
| | Structured-output artifacts | `repair()` strips `parsed` fields |
| | Invalid roles | Rejects unknown message roles |
| **Content blocks** | Provider format mismatch | `detect_format()` warns when messages don't match `target_provider` |
| | Thinking-block preservation | Flags Anthropic thinking blocks sent to OpenAI |
| | DeepSeek think extraction | Strips `<think>` tags from response text |
| | OpenAI function_call→tool_calls | Normalizes legacy format for Anthropic/Bedrock |
| | OpenAI reasoning blocks | Strips `reasoning` content blocks for Anthropic |
| **Multi-agent state** | Uncoordinated overwrites | `ScratchpadGuard` logs cross-agent key writes, reads, deletes |
| **Parallelism** | Out-of-order results | `ToolSequencer` flags results completing after later-started calls |

### Other guards

| Guard | What it does |
|-------|-------------|
| `AsyncClient` / `Client` | HTTP transport payload completeness (Content-Length, JSON truncation, empty body) |
| `Session` | Per-run cache isolation via `ContextVar` |
| `Audit log` | Every cache decision, stream event, and guard activity recorded |

## Audit log

Every cache decision is recorded:

```python
async with Session() as s:
    await fetch_customer(customer_id="c1")
    await fetch_customer(customer_id="c1")  # hit

for event in s.audit_log():
    print(event)
# {'event': 'cache_add', 'tool': 'fetch_customer', 'entity_id': 'c1', 'ts': ...}
# {'event': 'cache_hit', 'tool': 'fetch_customer', 'entity_id': 'c1', 'ts': ...}
```

## Performance

Benchmarked on Apple M-series (`python examples/benchmark_protect_decorator.py`):

| Pattern | Throughput |
|---|---|
| Cache hit (same entity) | ~300K ops/sec |
| Cache miss (entity churn) | ~190K ops/sec |
| Mixed (20 entities) | ~490K ops/sec |
| Concurrent (20 tasks × 500 calls) | ~300K ops/sec |
| TTL=0 worst case (always miss) | ~220K ops/sec |

The decorator adds a dict lookup and a `time.monotonic()` call on the hot path. Overhead is negligible compared to any real tool (HTTP call, DB query, etc.).

## Real-world validation

- **[agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006)** — test suite with 507 real failure cases, AutoGen #6789 and LiveKit #5408 real reproductions, and all 7 AF-006 manifestations tested end-to-end

## Synchronous frameworks

For frameworks that call tools synchronously (Smolagents, CrewAI's `BaseTool._run`), use `protect_sync`:

```python
from mycelium import protect_sync, Session
from mycelium.protect import _session_var

@protect_sync(entity_param="customer_id", ttl=60)
def fetch_customer(customer_id: str) -> dict:
    return db.get(customer_id)

session = Session()
token = _session_var.set(session)
try:
    result = fetch_customer(customer_id="c1")
finally:
    _session_var.reset(token)
```
