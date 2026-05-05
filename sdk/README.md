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

## Parameters

### `@protect`

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `entity_param` | `str \| None` | `None` | Kwarg name that identifies the entity. Different entity values get separate cache entries. |
| `ttl` | `float` | `300` | Seconds before a cached result is considered stale and the real function is called again. |
| `critical` | `bool` | `False` | Skip caching entirely — always call the real function. Use for tools where staleness is never acceptable. |

### `Session`

| Method | Description |
|--------|-------------|
| `session.cache_size()` | Number of live (non-expired) entries |
| `session.audit_log()` | Full list of cache events (`cache_add`, `cache_hit`, `cache_stale`, `cache_error`) |
| `session.invalidate(tool_name, entity_id)` | Manually evict a specific entry |

## What it protects against

All 7 AF-006 manifestations:

| Manifestation | How Mycelium prevents it |
|---|---|
| Stale data | TTL expiry — real function called after `ttl` seconds |
| Cross-entity leakage | Separate cache entry per `entity_param` value |
| Cross-source mixing | Separate cache entry per tool name |
| Behavioral drift | TTL forces re-fetch — drift surfaces on next call |
| Unbounded growth | Expired entries evicted automatically |
| Race conditions | Per-entity cache keys — concurrent calls never overwrite each other |
| Error invalidation | Any exception clears the entry — next call always gets fresh data |

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
