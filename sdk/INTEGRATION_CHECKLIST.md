# AF-006 Integration Checklist

Use this to confirm your agent is protected against context corruption (AF-006).

## Do you need protection?

- [ ] Agent uses external tools (APIs, databases, files)
- [ ] Multiple entities/users are involved (multi-customer, multi-tenant)
- [ ] Backend data can change mid-agent-loop
- [ ] Agent calls the same tool more than once per run

If ANY of the above is true, you need AF-006 protection.

---

## Setup

- [ ] Install: `pip install ./sdk`
- [ ] Import: `from mycelium import protect, Session`

---

## Decorate your tools

```python
from mycelium import protect, protect_sync

# Async tool
@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

# Sync tool (Smolagents, CrewAI BaseTool._run)
@protect_sync(entity_param="customer_id", ttl=60)
def fetch_customer_sync(customer_id: str) -> dict:
    return db.get(customer_id)
```

For each tool:
- [ ] Choose `ttl` based on data freshness requirements:
  - `10–30s` for highly dynamic data (inventory counts, balances)
  - `60–300s` for moderately dynamic data (user profile, order history)
  - `600s+` for slowly changing data (product catalog, settings)
- [ ] Set `entity_param` if the tool fetches per-entity data
- [ ] Set `critical=True` to skip caching entirely for side-effecting tools

---

## Wrap each agent run in a Session

```python
async with Session() as session:
    result = await fetch_customer(customer_id="c1")
    result2 = await fetch_customer(customer_id="c1")  # cache hit
```

- [ ] One `Session` per agent run — prevents cache leakage between runs
- [ ] Omit the session only for single-agent scripts (global session is fine there)

---

## Verify it's working

```python
async with Session() as session:
    r1 = await fetch_customer(customer_id="c1")
    # Mutate backend data here
    r2 = await fetch_customer(customer_id="c1")  # from cache

for event in session.audit_log():
    print(event)
```

- [ ] `cache_hit` events appear for repeated calls within TTL
- [ ] `cache_stale` events appear after TTL expires and data is re-fetched
- [ ] `cache_error` events appear when the tool raises (and entry is cleared)
- [ ] `session.cache_size()` stays bounded over time

---

## Verify all 7 failure modes

| Failure Mode | How to test | Expected |
|---|---|---|
| Stale data | Mutate backend, wait TTL, re-fetch | New data returned |
| Cross-entity leakage | Fetch entity A then B | Different results |
| Cross-source mixing | Two tools on same entity | Separate cache entries |
| Behavioral drift | Wait TTL, re-fetch | Fresh data |
| Unbounded growth | Long run | `cache_size()` stays bounded |
| Race conditions | Concurrent calls same entity | All return correct entity |
| Error invalidation | Tool raises, retry after | Fresh data, not stale pre-error value |

- [ ] All 7 verified in tests
- [ ] Run: `pytest tests/ -v` in [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006)
