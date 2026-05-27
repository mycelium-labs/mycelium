# AF-006 integration recipe

How to wire Mycelium for **context corruption (AF-006)** in a production agent loop.

Mycelium is **not** a magic “fix all agent errors” layer. It **prevents** some classes of bad context (stale tool cache), **repairs** a narrow set of message-shape bugs when you opt in, and **raises** when context is unsafe to send to the model.

---

## The three boundaries

| When | What | Behavior |
|------|------|----------|
| **Per agent run** | `async with Session():` | Isolates tool cache between runs/concurrent requests |
| **On every tool** | `@protect` / `protect_sync` | TTL refresh, per-entity keys, error invalidation, optional tenancy checks |
| **Before each LLM call** | `MessageValidator.repair()` (or `.validate()`) | Fix or reject broken message history |

Optional guards at the same boundaries:

- `HistoryGuard` — history too large, silent drops, duplicate turns
- `StreamGuard` — cut-off streams, duplicate chunks
- `ContentBlockNormalizer` — provider format mismatches
- `mycelium.http.AsyncClient` / `Client` — truncated HTTP tool payloads

---

## Minimal loop (async)

```python
from mycelium import MessageValidator, protect, Session

validator = MessageValidator()

@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return await crm.get(customer_id)

async def agent_turn(messages: list, customer_id: str) -> list:
    async with Session():
        # 1) Tools — cache + freshness
        customer = await fetch_customer(customer_id=customer_id)

        messages.append({"role": "tool", "tool_call_id": "…", "content": str(customer)})

    # 2) Before LLM — shape + orphans (repair what we can)
    messages = validator.repair(messages)
    # Or strict: validator.validate(messages)  # raises MessageValidationError

    # 3) Call your model with messages
  # response = await llm.ainvoke(messages)
    return messages
```

Use `validator.validate()` when you want to **stop and handle** (retry, rebuild history, alert). Use `repair()` when you want to **fix known mechanical bugs** (duplicate `fc_*` partials, bad indices) and only fail on unfixable orphans.

---

## What each layer does

### `@protect` + `Session` — **prevent** stale/wrong tool cache

- **Prevents:** serving expired tool results, cross-entity cache bleed, caching after tool errors
- **Does not:** fix truncated JSON, orphaned tool results in history, RAG drift, subgraph state loss

```python
@protect(entity_param="customer_id", ttl=60, read_after_write_grace=2.0)
async def get_balance(customer_id: str) -> dict: ...

@protect(entity_param="customer_id", mark_as_write=True)
async def update_balance(customer_id: str, amount: float) -> dict: ...
```

Inspect decisions: `session.audit_log()` → `cache_hit`, `cache_stale`, `cache_error`, etc.

### `MessageValidator` — **flag or repair** transcript shape

- **`validate()`** → raises `MessageValidationError` (orphaned tool results, bad roles, …)
- **`repair()`** → drops duplicate streaming partials, fixes indices; still raises if unfixable

This targets **message-format** AF-006 issues. It does not refetch stale CRM data — that is `@protect`.

### Other guards (opt-in)

| Guard | Prevent | Flag (raise) |
|-------|---------|----------------|
| `HistoryGuard` | — | Oversized history, silent drops, duplicate turns |
| `StreamGuard` | Duplicate chunks (drop) | `StreamCutOffError` if stream ends without stop |
| `ContentBlockNormalizer` | Strips/normalizes some blocks | `ContentBlockError` when unsafe |
| `AsyncClient` | — | `PayloadIncompleteError` on truncated HTTP body |

---

## Full-stack recipe (recommended for AF-006-heavy agents)

```python
from mycelium import (
    HistoryGuard,
    MessageValidator,
    StreamGuard,
    protect,
    Session,
)

validator = MessageValidator()
history_guard = HistoryGuard(max_messages=200, detect_duplicates=True)

@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return await crm.get(customer_id)

async def run_turn(messages: list, customer_id: str) -> list:
    history_guard.check_for_drops(messages)
    history_guard.validate(messages)

    async with Session() as session:
        row = await fetch_customer(customer_id=customer_id)
        messages.append({"role": "tool", "tool_call_id": "call_1", "content": str(row)})
        # log: session.audit_log()

    messages = validator.repair(messages)
    return messages

async def stream_llm(prompt: str):
    async with StreamGuard(format="openai") as guard:
        async for chunk in llm.astream(prompt):
            chunk = guard.process(chunk)
            if chunk is not None:
                yield chunk
```

Not every agent needs every guard. Start with **Session + @protect + MessageValidator** before the LLM call.

---

## Sync tools (CrewAI, Smolagents)

```python
from mycelium import protect_sync, Session
from mycelium.protect import _session_var

@protect_sync(entity_param="sku", ttl=120)
def get_inventory(sku: str) -> dict:
    return warehouse.get(sku)

session = Session()
token = _session_var.set(session)
try:
    stock = get_inventory(sku="SKU-1")
finally:
    _session_var.reset(token)
```

(`Session` is async-context-manager only; sync frameworks bind it via `_session_var`.)

---

## Out of scope for this recipe (~140/507 corpus issues)

Issues labeled AF-006 in the wild but **not** fixed by tool cache + message repair alone:

- Framework checkpoint / subgraph state loss
- RAG retrieval drift and poisoned retrieved content (overlaps AF-009)
- Vision/UI misreads, file encoding, infra canaries
- Pure framework bugs (namespace drops, thinking-block stripping in the orchestrator)

For those: fix upstream, or wire `HistoryGuard` / `StreamGuard` / `ContentBlockNormalizer` where the failure actually occurs. See `research/context-corruption-taxonomy.md`.

---

## Proof and CI

- Unit tests: `sdk/tests/`
- Proof suite: [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006) (runs on every Mycelium PR via `.github/workflows/proof.yml`)
- **507 corpus:** classification + mechanism tests when HF cache is present — **not** 507 full agent reproductions per commit. Details in agent-test-AF006 `README.md`.

---

## Related docs

- [sdk/README.md](../sdk/README.md) — API reference
- [research/context-corruption-taxonomy.md](../research/context-corruption-taxonomy.md) — coverage map
- [AF-006-DESIGN.md](./AF-006-DESIGN.md) — historical design (includes legacy runtime; prefer this doc for integration)
