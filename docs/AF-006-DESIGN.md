# AF-006: Context Corruption Protection

## Overview

Protects agents from reasoning over stale, poisoned, or cross-contaminated context.

**Core principle**: Treat context as a cache with explicit TTLs, versioning, and strict segmentation.

---

## Architecture

### Three Layers

```
┌─────────────────────────────────────────────────────────────┐
│ AGENT CODE (Developer writes this)                          │
│                                                             │
│  async def my_agent(runtime):                              │
│    user = await runtime.call_tool("fetch_user", user_id) │
│    runtime.advance_step()                                  │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ RUNTIME (AgentRuntimeWithContextProtection)                │
│                                                             │
│  1. call_tool(name, func, **kwargs)                        │
│     - Looks up metadata in registry                        │
│     - Checks cache, decides: USE / REFETCH / NOT_CACHED   │
│     - Calls tool or returns cached value                   │
│     - Stores result with criticality/TTL                   │
│                                                             │
│  2. advance_step()                                          │
│     - Increments step counter                              │
│     - Triggers TTL checks on next cache access             │
│                                                             │
│  3. get_cache_snapshot() / get_audit_log()                 │
│     - Debugging and observability                          │
└─────────────────────────────────────────────────────────────┘
                          ↓
┌─────────────────────────────────────────────────────────────┐
│ CACHE (ContextCache)                                        │
│                                                             │
│  Maintains immutable versioned state:                      │
│  - ContextEntryHistory (immutable append-only)             │
│  - Audit log (every operation logged)                      │
│                                                             │
│  Operations:                                               │
│  - add(name, value, source, entity_id, criticality, ttl)  │
│  - get(name, source, entity_id) → AccessDecision          │
│  - advance_step()                                          │
│  - invalidate_on_error(source, error, entity_id)          │
│                                                             │
│  Returns explicit decisions:                               │
│  - should_refetch: True/False                              │
│  - reason: Why (stale, criticality, missing, etc)         │
│  - version_id: Which version is current                    │
│  - access_count: How many times read                       │
│  - age_steps: Steps since added                            │
└─────────────────────────────────────────────────────────────┘
```

---

## Tool Lifecycle

### 1. Tool Registration (One-time)

```python
@tool(
    critical=True,
    entity_param="user_id",
    invalidate_after_steps=5
)
def fetch_user(user_id: str) -> dict:
    return api.get(f"/users/{user_id}")

runtime = AgentRuntimeWithContextProtection()
runtime.register_tools([fetch_user])
```

Metadata stored in registry:
```
fetch_user → ToolMetadata(
    critical=True,
    entity_param="user_id",
    invalidate_after_steps=5
)
```

### 2. Tool Invocation (Per call)

```
Step 1: Agent calls runtime
  agent → runtime.call_tool("fetch_user", fetch_user, user_id="alice")

Step 2: Runtime looks up metadata
  registry.get("fetch_user") → ToolMetadata(critical=True, ...)

Step 3: Runtime extracts entity_id
  tool_kwargs = {"user_id": "alice"}
  entity_id = "alice" (from metadata.entity_param)

Step 4: Runtime checks cache
  decision = cache.get("fetch_user", "fetch_user", "alice")

Step 5a: Cache HIT (fresh)
  decision.should_refetch = False
  runtime returns cached value
  agent gets result

Step 5b: Cache MISS or REFETCH needed
  decision.should_refetch = True
  reason = "stale" OR "repeated_read" OR "missing"

Step 6: Call tool
  result = await fetch_user(user_id="alice")

Step 7: Store in cache
  cache.add(
    name="fetch_user",
    value=result,
    source="fetch_user",
    entity_id="alice",
    criticality=Criticality.HIGH,
    invalidate_after_steps=5
  )

Step 8: Return to agent
  agent ← result
```

### 3. Step Advancement

```
Agent finishes reasoning step
  ↓
Agent calls runtime.advance_step()
  ↓
Runtime increments internal step counter
  ↓
Cache re-evaluates freshness on next access
  Example: Entry added at step 0, TTL=5
  - At step 4: age=4, fresh
  - At step 5: age=5, STALE (age >= TTL)
  - Next call: cache.get() triggers refetch
```

### 4. Error Handling

```
Tool call raises exception
  ↓
Runtime catches exception
  ↓
Runtime calls cache.invalidate_on_error(source, error, entity_id)
  ↓
Cache checks: Is this a rate-limit error?
  ↓
  If YES: Record as RATE_LIMITED, remove entry
  → Agent can retry after delay
  ↓
  If NO: Record as ERROR, remove entry
  → Agent decides on retry (probably don't retry)
  ↓
Runtime re-raises exception to agent
```

---

## Validation Rules

### 1. TTL (Time-to-Live)

```
age_steps = current_step - entry.created_at_step

if age_steps >= entry.invalidate_after_steps:
    should_refetch = True
    reason = "Stale"
```

**Developer decides per-tool**:
```python
@tool(invalidate_after_steps=1)   # Always fresh
def get_quota(): ...

@tool(invalidate_after_steps=999)  # Rarely changes
def get_static_config(): ...
```

### 2. Criticality + Repeated Read

```
if entry.criticality == HIGH and access_count >= 2:
    should_refetch = True
    reason = "High criticality + repeated read"
```

**Use for**: User IDs, API keys, resource handles
**Why**: If agent reads twice, likely depends on it → verify freshness

### 3. Segmentation (Cross-Entity Isolation)

```
ContextSegmentation.ENTITY:
  key = f"{entity_id}:{name}"
  fetch_user(user_id="alice") → alice:fetch_user
  fetch_user(user_id="bob")   → bob:fetch_user
  ✓ No cross-entity leakage

ContextSegmentation.SOURCE:
  key = f"{source}:{name}"
  fetch_user() → fetch_user:fetch_user
  search()    → search:search
  ✓ No cross-source leakage

ContextSegmentation.BOTH:
  key = f"{entity_id}:{source}:{name}"
  Maximum isolation (recommended)
```

### 4. Rate-Limit Detection

```
Tool errors with: "Rate limit exceeded (429)"
  ↓
Runtime calls cache._is_rate_limit_error(error)
  ↓
Checks against patterns:
  - r"(?i)(rate.?limit|quota|429|too.?many.?requests|throttl)"
  - r"(?i)(please.?wait|retry.?after|backoff|delay)"
  - r"(?i)(concurrent.?request|limit.?exceeded|exceeded.?limit)"
  - r"(?i)(too.?many|maximum.?allowed|peak|capacity)"
  - r"status.?code.*429"
  ↓
  If match: is_rate_limit = True
  ✓ Can auto-retry after delay
  ↓
  If no match: is_rate_limit = False
  ✓ Don't retry, let agent decide
```

---

## Data Flow Example

### Agent Run: 11 Steps

```
┌─────────────────────────────────────────────────────────────────────┐
│ STEP 1: Fetch user alice                                            │
├─────────────────────────────────────────────────────────────────────┤
│ call_tool("fetch_user", user_id="alice")                           │
│ → cache miss (not in cache)                                         │
│ → fetch_user("alice") called                                        │
│ → result = {"id": "alice", "name": "Alice"}                        │
│ → cache.add(name="fetch_user", entity_id="alice", value=result)   │
│ → version_id = "v1"                                                 │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ STEP 2: Search documents                                            │
├─────────────────────────────────────────────────────────────────────┤
│ call_tool("search_documents", query="ml")                           │
│ → cache miss (not in cache)                                         │
│ → search_documents("ml") called                                     │
│ → result = [doc1, doc2, doc3]                                       │
│ → cache.add(name="search_documents", value=result)                 │
│ → version_id = "v1"                                                 │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ STEP 3: Fetch user again (within 5 step TTL)                       │
├─────────────────────────────────────────────────────────────────────┤
│ call_tool("fetch_user", user_id="alice")                           │
│ → cache.get("fetch_user", entity_id="alice")                       │
│ → age = 3 - 1 = 2 steps                                             │
│ → 2 < TTL(5) ✓ Fresh                                                │
│ → not HIGH criticality OR access_count < 2 ✓                       │
│ → CACHE HIT, return {"id": "alice", "name": "Alice"}              │
│ → no fresh call needed                                              │
│ → reason: "Fresh and safe"                                          │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ STEPS 4-8: Idle reasoning                                           │
├─────────────────────────────────────────────────────────────────────┤
│ runtime.advance_step() called 5 times                               │
│ internal step counter: 8                                            │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ STEP 9: Fetch user again (NOW STALE)                               │
├─────────────────────────────────────────────────────────────────────┤
│ call_tool("fetch_user", user_id="alice")                           │
│ → cache.get("fetch_user", entity_id="alice")                       │
│ → age = 9 - 1 = 8 steps                                             │
│ → 8 >= TTL(5) ✗ STALE                                              │
│ → CACHE MISS, reason: "Stale (age 8 >= TTL 5)"                    │
│ → fetch_user("alice") called (fresh)                               │
│ → result = {"id": "alice", "name": "Alice", "updated": true}      │
│ → cache.add(...) creates new version v2                            │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ STEP 10: Tool error (rate-limit)                                    │
├─────────────────────────────────────────────────────────────────────┤
│ call_tool("fetch_user", user_id="bob")                             │
│ → cache miss                                                        │
│ → fetch_user("bob") called                                          │
│ → raises Exception("Rate limit exceeded (429)")                     │
│ → cache.invalidate_on_error("fetch_user", error, entity_id="bob") │
│ → matches rate-limit pattern → is_rate_limit = True                │
│ → removed bob's entry from cache                                    │
│ → Exception re-raised to agent                                      │
│ → agent sees: rate-limit error, can retry later                    │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ STEP 11: Fetch bob again (after rate-limit)                        │
├─────────────────────────────────────────────────────────────────────┤
│ call_tool("fetch_user", user_id="bob")                             │
│ → cache miss (invalidated in step 10)                              │
│ → fetch_user("bob") called (retry)                                 │
│ → succeeds this time                                                │
│ → cache.add(name="fetch_user", entity_id="bob", value=result)     │
│ → version_id = "v1"                                                 │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Developer Experience

### Step 1: Mark Tools

```python
from mycelium.protections import tool

@tool(
    critical=True,                    # Re-verify if read 2+ times
    entity_param="user_id",           # Entity scoping
    invalidate_after_steps=5          # TTL in steps
)
def fetch_user(user_id: str) -> dict:
    return api.get(f"/users/{user_id}")
```

### Step 2: Create Runtime

```python
from mycelium.core import AgentRuntimeWithContextProtection, InvalidationPolicy, ContextSegmentation

runtime = AgentRuntimeWithContextProtection(
    policy=InvalidationPolicy(
        default_ttl_steps=5,
        criticality_recheck_threshold=2,
        segmentation=ContextSegmentation.BOTH
    ),
    verbose=True  # See cache decisions
)
runtime.register_tools([fetch_user, search_docs, get_quota])
```

### Step 3: Use in Agent

```python
async def my_agent(runtime):
    for step in range(max_steps):
        # Reasoning...
        decision = await agent_reasoning()

        # Tool call (automatic caching)
        user = await runtime.call_tool(
            "fetch_user",
            fetch_user,
            user_id="alice"
        )

        # Step advancement (triggers TTL checks)
        runtime.advance_step()

    return result
```

### Step 4: Observe

```python
# Get cache state
snapshot = runtime.get_cache_snapshot()
print(snapshot)

# Get audit trail
audit = runtime.get_audit_log()
for event in audit:
    print(f"{event['event_type']}: {event['data']}")
```

---

## Safety Guarantees

| Threat | Protection | How |
|--------|-----------|-----|
| **Stale data** | TTL enforcement | Age checked on every access |
| **Cross-entity leakage** | Segmentation | Entity ID in cache key |
| **Cross-source leakage** | Segmentation | Source in cache key |
| **Invalid results from errors** | Error invalidation | Immediately removed from cache |
| **Rate-limit loops** | Rate-limit aware | Detects via regex, distinguishes from failures |
| **Silent corruption** | Audit trail | Every operation logged |
| **Data mutation** | Immutability | Versions are frozen, new updates create new versions |
| **Unbounded growth** | Explicit invalidation | Errors remove entries, TTL enforces expiry |

---

## Next Steps

1. **Integration**: Wire into your agent framework (LangGraph, CrewAI, etc)
2. **Policy tuning**: Adjust TTLs based on dogfooding
3. **Observability**: Add traces/metrics to see cache hit rates
4. **Other failure modes**: AF-001 through AF-009
