# AF-006 Integration Checklist

Use this checklist to ensure your agent is fully protected against context corruption (AF-006).

## ✅ Pre-Integration Assessment

- [ ] Agent uses external tools (APIs, databases, files, search engines)
- [ ] Multiple entities/users are involved (multi-customer, multi-tenant)
- [ ] Tools can return stale data (no cache busting mechanism)
- [ ] Agent runs for 5+ reasoning steps
- [ ] Backend data can change mid-agent-loop
- [ ] Critical operations that need fresh data are repeated (e.g., fetch user profile twice)

If ANY of the above is true, you need AF-006 protection.

---

## ✅ Installation & Setup

- [ ] Install Mycelium SDK: `pip install ./sdk` (or `pip install -e ./sdk` for development)
- [ ] Choose your framework integration:
  - [ ] LangGraph (`mycelium.adapters.langgraph`)
  - [ ] CrewAI (`mycelium.adapters.crewai`)
  - [ ] AutoGen (`mycelium.adapters.autogen`)
  - [ ] OpenAI Agents (`mycelium.adapters.openai_agents`)
  - [ ] Smolagents (`mycelium.adapters.smolagents`)
- [ ] Import integration: `from mycelium.adapters.<framework> import <Framework>Integration`

---

## ✅ Tool Decoration

For each tool in your agent:

- [ ] Add `@tool` decorator from `mycelium.protections.decorators`
- [ ] Identify if tool returns **critical data** (user IDs, api keys, permissions)
  - [ ] Set `critical=True` if yes, `critical=False` if no
- [ ] Set `invalidate_after_steps=N` based on data freshness requirements
  - [ ] Use `1-2` for highly dynamic data (user preferences, inventory counts)
  - [ ] Use `5-10` for moderately dynamic data (user profile, order history)
  - [ ] Use `15+` for slowly changing data (product catalog, settings)
- [ ] Identify the entity parameter (if applicable)
  - [ ] Set `entity_param="user_id"` (or equivalent) to enable entity segmentation
  - [ ] Leave as `None` for global-scope tools (list all users, system status)
- [ ] Verify custom rate-limit patterns (optional)
  - [ ] Set `rate_limit_pattern=r"(rate|quota|429)"` if your API uses custom error messages

**Example**:
```python
from mycelium.protections.decorators import tool

@tool(
    critical=True,                  # User data is critical
    invalidate_after_steps=5,       # Refetch every 5 steps
    entity_param="user_id",         # Segment by user
    rate_limit_pattern=r"(429|rate.?limit)"
)
async def fetch_user_profile(user_id: str) -> dict:
    return api.get(f"/users/{user_id}")
```

---

## ✅ Integration Setup

In your agent code:

- [ ] Create integration instance:
  ```python
  from mycelium.adapters.langgraph import LangGraphIntegration
  integration = LangGraphIntegration()
  ```

- [ ] Register tools:
  ```python
  integration.register_tools({
      "fetch_user_profile": fetch_user_profile,
      "get_order_history": get_order_history,
      "send_email": send_email,  # Non-critical tools too
  }, critical_tools=["fetch_user_profile", "get_order_history"])
  ```

- [ ] Get protection instance:
  ```python
  protection = integration.get_protection()
  ```

---

## ✅ Tool Calls

Replace all direct tool calls with protected calls:

**Before**:
```python
user = await fetch_user_profile(user_id="alice")
```

**After**:
```python
user = await protection.call_tool_protected(
    "fetch_user_profile",
    fetch_user_profile,
    user_id="alice"
)
```

Requirements:
- [ ] Use async/await (or convert sync tools to async)
- [ ] Pass tool name as string (must match registration)
- [ ] Pass tool function reference
- [ ] Pass all tool kwargs by name

---

## ✅ Step Advancement

After each agent reasoning step, call `advance_step()`:

**Before**:
```python
for step in range(max_steps):
    decision = await agent.think()
    if decision == "fetch_user":
        user = await fetch_user_profile(user_id="alice")
    # Missing: step advancement
```

**After**:
```python
for step in range(max_steps):
    decision = await agent.think()
    if decision == "fetch_user":
        user = await protection.call_tool_protected(...)
    protection.advance_step()  # Required!
```

Critical:
- [ ] Call `advance_step()` exactly once per reasoning cycle
- [ ] Call it AFTER all tool calls in that step
- [ ] Call it even if no tools were called (idle step)

---

## ✅ Configuration (Optional)

To customize cache behavior:

```python
from mycelium.core.runtime_context_corruption import InvalidationPolicy
from mycelium.protections.context_corruption import ContextSegmentation

policy = InvalidationPolicy(
    default_ttl_steps=5,                # Default TTL for tools without @tool
    criticality_recheck_threshold=2,    # Re-verify critical after 2 reads
    segmentation=ContextSegmentation.BOTH,  # Entity + source segmentation
    rate_limit_patterns=[r"(429|rate)"]     # Regex patterns for rate-limits
)

integration = LangGraphIntegration(policy=policy, verbose=True)
```

- [ ] Adjust `default_ttl_steps` if most tools need different TTL
- [ ] Leave `criticality_recheck_threshold=2` (proven default)
- [ ] Use `ContextSegmentation.BOTH` for multi-user agents (recommended)
- [ ] Set `verbose=True` during development to see cache decisions

---

## ✅ Monitoring & Debugging

Use these methods to observe cache behavior:

```python
# Get cache statistics
stats = protection.get_stats()
print(f"Cache hits: {stats['cache_hits']}")
print(f"Cache misses: {stats['cache_misses']}")
print(f"Hit rate: {stats['hit_rate']:.1%}")
print(f"Steps: {stats['steps']}")

# Get cache state snapshot (for debugging)
snapshot = protection.get_cache_snapshot()
print(f"Cached entries: {len(snapshot)}")

# Get complete audit trail
audit = protection.get_audit_log()
for event in audit:
    if event['event_type'] in ('get_hit', 'get_stale', 'get_repeated_read'):
        print(f"{event['event_type']}: {event['data']}")
```

Checklist:
- [ ] Check hit rate is 30-70% (not 0% or 100%)
  - 0% = all cache misses (TTL too aggressive)
  - 100% = all hits (TTL too lenient, stale data risk!)
  - 30-70% = balanced, some cache benefit with freshness
- [ ] Monitor audit log for `get_repeated_read` events (criticality re-verification working)
- [ ] Check `get_stale` events appear regularly (TTL invalidation working)
- [ ] Verify cache size stays bounded (observe snapshot length over time)

---

## ✅ Testing Your Integration

To verify AF-006 protection is working:

### 1. Create a test that mutates backend data mid-loop

```python
@pytest.mark.asyncio
async def test_critical_tool_detects_stale_data():
    # Fetch user (cached)
    user1 = await protection.call_tool_protected(...)
    initial_email = user1["email"]

    # Mutate backend
    backend.update_user(user_id="alice", email="newemail@example.com")

    # Advance steps and fetch again
    for _ in range(5):  # Past TTL
        protection.advance_step()

    # Should refetch and see new email
    user2 = await protection.call_tool_protected(...)
    assert user2["email"] == "newemail@example.com"
```

- [ ] Test detects stale data (mutation not seen without SDK)
- [ ] Test passes with SDK (mutation detected after TTL)

### 2. Create a multi-entity test

```python
@pytest.mark.asyncio
async def test_entity_isolation():
    # Fetch multiple customers
    user_a = await protection.call_tool_protected(..., customer_id="A")
    user_b = await protection.call_tool_protected(..., customer_id="B")

    # Results must be different
    assert user_a["customer_id"] != user_b["customer_id"]
    assert user_a["email"] != user_b["email"]
```

- [ ] Test verifies entity segmentation works

### 3. Create a concurrency test

```python
@pytest.mark.asyncio
async def test_concurrent_access_no_corruption():
    tasks = [
        protection.call_tool_protected(..., customer_id="A")
        for _ in range(100)
    ]
    results = await asyncio.gather(*tasks)

    # All results should be customer A
    for result in results:
        assert result["customer_id"] == "A"
```

- [ ] Test verifies no race conditions

---

## ✅ Production Deployment

Before going to production:

- [ ] All tools are decorated with `@tool`
- [ ] All tool calls use `call_tool_protected()`
- [ ] `advance_step()` called once per reasoning cycle
- [ ] Hit rate is in the 30-70% range (healthy balance)
- [ ] Audit log shows regular `get_stale` events (TTL working)
- [ ] No `get_repeated_read` events for non-critical tools
- [ ] Cache size stays bounded over time (no memory leaks)
- [ ] Tests pass: entity isolation, stale data detection, concurrency
- [ ] Set `verbose=False` for production (reduces logging overhead)
- [ ] Monitor cache stats in production (add to observability)

---

## ✅ Verification Against All 7 Failure Modes

Use this final checklist to confirm all AF-006 failure modes are prevented:

| Failure Mode | Test | Expected Result |
|---|---|---|
| **Stale Data** | Mutate backend, advance N steps, fetch | New data returned ✅ |
| **Cross-Entity Leakage** | Fetch entity A then B | Different results ✅ |
| **Cross-Source Mixing** | Use 2+ tools on same entity | Separate cache entries ✅ |
| **Behavioral Drift** | Read critical data 3x | Refetch on 3rd read ✅ |
| **Unbounded Growth** | Run 1000 steps | Cache size bounded ✅ |
| **Race Conditions** | 100 concurrent calls | All return correct entity ✅ |
| **Error Invalidation** | Tool fails, retry | Cache cleared on error ✅ |

- [ ] Stale data test passes
- [ ] Cross-entity isolation test passes
- [ ] Cross-source mixing test passes
- [ ] Behavioral drift test passes
- [ ] Unbounded growth test passes (monitor memory)
- [ ] Race condition test passes (or documented safe patterns)
- [ ] Error invalidation test passes (if tool errors possible)

---

## ✅ Reference Documentation

For more details:

- **[PROOF_SUMMARY.md](PROOF_SUMMARY.md)** — Complete proof across all 7 failure modes
- **[AF006_PROOF.md](AF006_PROOF.md)** — Test matrix and coverage details
- **[README.md](README.md)** — Usage examples and framework guides
- **[agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006)** — Real-world comparison agent

---

## ✅ Support & Validation

Questions? Use these resources:

- **See working example**: [agent-test-AF006 repo](https://github.com/mycelium-labs/agent-test-AF006)
- **Run full test suite**: `pytest tests/ -v` (all 600+ test cases)
- **Read proof documentation**: [PROOF_SUMMARY.md](PROOF_SUMMARY.md)
- **Check framework guides**: [README.md Usage section](README.md#usage)

---

**Status**: When all checkboxes are complete, your agent is fully protected against AF-006 ✅
