# Mycelium

Runtime protection for AI agents against context corruption (AF-006).

Agents break when tool results go stale, leak across entities, or get dropped from context windows. Mycelium catches this at the tool level — developers decorate their functions once and use them normally in any framework.

---

## The problem

```python
# Without Mycelium — stale data served silently
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

result = await fetch_customer("c1")  # cached by LangGraph/AutoGen/etc.
# ... 10 minutes later, customer changed plans ...
result = await fetch_customer("c1")  # still returns 10-minute-old data
```

```python
# With Mycelium — stale data detected and refreshed
from mycelium import protect

@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

result = await fetch_customer("c1")  # fresh
# ... 60 seconds pass ...
result = await fetch_customer("c1")  # Mycelium detects stale, calls DB again
```

No new calling convention. No adapter imports. The tool works exactly the same in LangGraph, AutoGen, CrewAI, Pydantic AI, LangChain, or any other framework.

---

## Install

```bash
pip install ./sdk
```

---

## Quick start

```python
from mycelium import protect, Session

@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

@protect(entity_param="sku", ttl=300)
async def get_inventory(sku: str) -> dict:
    return await warehouse.get(sku)

# Use in any framework, completely unchanged
async with Session():
    customer = await fetch_customer(customer_id="c1")
    inventory = await get_inventory(sku="SKU-99")
```

See [sdk/README.md](sdk/README.md) for full documentation.

---

## What it protects against

7 AF-006 manifestations, all proven in [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006):

| Manifestation | Protection |
|---|---|
| Stale data | TTL expiry — real function called after `ttl` seconds |
| Cross-entity leakage | Separate cache entry per `entity_param` value |
| Cross-source mixing | Separate cache entry per tool name |
| Behavioral drift | TTL forces re-fetch — drift surfaces on next call |
| Unbounded growth | Expired entries evicted automatically on each step |
| Race conditions | Per-entity keys — concurrent calls never overwrite each other |
| Error invalidation | Any exception clears the entry immediately |

---

## Real-world validation

**[agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006)** contains:

- **AutoGen #6789** — real `TokenLimitedChatCompletionContext` reproduction. Middle-deletion removes `AssistantMessage(FunctionCall)` while `FunctionExecutionResultMessage` survives, producing the orphaned tool_result that causes 400 API errors. 10 tests, all using real `autogen_core` classes.
- **LiveKit #5408** — real redundant-signal and transcript-discard bug pattern. 10 tests.
- **507 real failures** loaded from `ndileep/mycelium-agent-failures` on HuggingFace — real GitHub issues from LangGraph, CrewAI, AutoGen, Cline, OpenHands and others.
- **All 7 manifestations** tested end-to-end with the `@protect` decorator.

---

## Framework support

Confirmed end-to-end with real framework invocation paths:

| Framework | Invocation path |
|---|---|
| LangGraph | `StateGraph.compile().ainvoke()` |
| AutoGen | `FunctionCall` → executor |
| CrewAI | `BaseTool._run(**calling.arguments)` |
| Smolagents | `Tool.forward()` |
| OpenAI Agents | `FunctionTool.on_invoke_tool()` |
| LiveKit Agents | `execute_function_call()` |
| LangChain | `tool.ainvoke(args_dict)` |
| Pydantic AI | `FunctionSchema.call()` → `await function(**kwargs)` |

DSPy and Haystack follow the same async function call pattern — tests not yet written.

---

## Repository layout

```
sdk/                          Python package (pip install ./sdk)
  mycelium/
    protect.py                @protect / protect_sync decorators and Session
    protections/
      context_corruption.py   ContextCache: TTL, versioning, eviction, audit
    core/
      runtime_context_corruption.py  Step-based runtime (advanced use)

research/                     AF-006 and other failure mode analysis
incidents/                    Tagged real incidents from production agents
```

---

## All 9 agent failure modes

Mycelium documents and protects against 9 distinct failure modes observed in production agents:

| # | Mode | Occurrences | Status |
|---|------|-------------|--------|
| AF-001 | Hallucination Cascade | 36 | Documented |
| AF-002 | Observability Black Hole | 304 | Planned v2 |
| AF-003 | Infinite Reasoning Loops | 218 | Documented |
| AF-004 | Tool Misuse | 575 | Planned v2 |
| AF-005 | Goal Misalignment | 177 | Documented |
| **AF-006** | **Context Corruption** | **501** | **Complete** |
| AF-007 | Premature Termination | 415 | Documented |
| AF-008 | Cascading Permission | 9 | Documented |
| AF-009 | Instruction Injection | 22 | Documented |

Frequencies from `ndileep/mycelium-agent-failures` on HuggingFace.
