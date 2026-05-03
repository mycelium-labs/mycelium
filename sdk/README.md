# Mycelium SDK (Python)

Runtime protection library for AI agents against context corruption failure modes (AF-006).

## Quick Start

From this directory:

```bash
uv sync --all-groups
uv run pytest
uv run python -c "import mycelium; print(mycelium.__version__)"
```

Uses **`sdk/.venv`** (separate from a repo-root `.venv` if you have one).

## Installation

Install the SDK as a library in your agent project:

```bash
# From the mycelium repo
pip install ./sdk

# Or editable install for development
pip install -e ./sdk
```

## Usage

### Basic Usage with LangChain/LangGraph

```python
from mycelium.adapters.langgraph import LangGraphIntegration
from mycelium.core.runtime_context_corruption import AgentRuntimeWithContextProtection

# Create protection for your agent
integration = LangGraphIntegration()

# Define a tool
def fetch_user(user_id: str) -> dict:
    return {"id": user_id, "name": "User"}

# Register with protection rules
integration.register_tools(
    {"fetch_user": fetch_user},
    critical_tools=["fetch_user"]  # Mark critical data
)

# Use in your agent
protection = integration.get_protection()

# Call tools through protection
result = await protection.call_tool_protected(
    "fetch_user",
    fetch_user,
    user_id="alice"
)

# Advance step after each agent reasoning step
protection.advance_step()

# Get statistics
stats = integration.get_stats()
print(f"Cache hit rate: {stats['hit_rate']}")
```

### Framework Integrations

The SDK supports multiple agent frameworks:

#### LangGraph
```python
from mycelium.adapters.langgraph import LangGraphIntegration

integration = LangGraphIntegration()
integration.register_tools({"tool_name": tool_func})
protection = integration.get_protection()
```

#### CrewAI
```python
from mycelium.adapters.crewai import CrewAIIntegration

integration = CrewAIIntegration()
integration.register_tools({"tool_name": tool_func})
protection = integration.get_protection()
```

#### AutoGen
```python
from mycelium.adapters.autogen import AutoGenIntegration

integration = AutoGenIntegration()
integration.register_tools({"tool_name": tool_func})
protection = integration.get_protection()
```

#### OpenAI Agents SDK
```python
from mycelium.adapters.openai_agents import OpenAIAgentsIntegration

integration = OpenAIAgentsIntegration()
integration.register_tools({"tool_name": tool_func})
protection = integration.get_protection()
```

#### Smolagents
```python
from mycelium.adapters.smolagents import SmolagentsIntegration

integration = SmolagentsIntegration()
integration.register_tools({"tool_name": tool_func})
protection = integration.get_protection()
```

### Protecting Tools

Mark tools with protection rules using decorators:

```python
from mycelium.protections.decorators import tool

@tool(
    critical=True,                 # HIGH criticality → force re-verify on repeated reads
    entity_param="user_id",        # Parameter name that identifies the entity
    invalidate_after_steps=5       # Refetch after 5 agent reasoning steps
)
async def fetch_user_profile(user_id: str) -> dict:
    """Fetch user profile (must stay fresh)."""
    return api.get(f"/users/{user_id}")

@tool(
    critical=False,
    invalidate_after_steps=10      # Less critical, can use older cache
)
async def search_documents(query: str) -> list[dict]:
    """Search documents."""
    return db.search(query)
```

### Configuration

Customize context invalidation rules:

```python
from mycelium.core.runtime_context_corruption import (
    AgentRuntimeWithContextProtection,
    InvalidationPolicy,
)
from mycelium.protections.context_corruption import ContextSegmentation

policy = InvalidationPolicy(
    default_ttl_steps=5,                           # Default cache TTL
    criticality_recheck_threshold=2,               # Re-verify high-criticality after 2+ reads
    segmentation=ContextSegmentation.BOTH,         # Separate cache by entity AND source
    rate_limit_patterns=[r"(?i)(rate.?limit|429)"] # Custom rate-limit detection
)

runtime = AgentRuntimeWithContextProtection(policy=policy, verbose=True)
```

### Monitoring

Access cache statistics and audit logs:

```python
# Get current cache state
snapshot = protection.get_cache_snapshot()
print(f"Cached entries: {len(snapshot)}")

# Get audit trail
audit = protection.get_audit_log()
for event in audit:
    print(f"{event['event_type']}: {event['data']}")

# Get stats
stats = protection.get_stats()
print(f"Hits: {stats['cache_hits']}, Misses: {stats['cache_misses']}")
print(f"Hit rate: {stats['hit_rate']:.1%}")
```

## Core Concepts

### Context Corruption (AF-006)

Agents can suffer from context corruption when:
- **Stale data**: Using old cached context without re-verification
- **Cross-contamination**: Mixing context from different entities/sources
- **Behavioral drift**: High-criticality data read repeatedly without re-check

### Protection Mechanisms

1. **TTL-based invalidation**: Auto-refetch after N reasoning steps
2. **Criticality re-verification**: Force re-check high-criticality data on repeated reads
3. **Entity segmentation**: Separate caches by entity_id (e.g., user_id)
4. **Source segmentation**: Separate caches by tool/source
5. **Immutable versioning**: All entries are append-only with complete audit trail
6. **Error invalidation**: Immediately invalidate on tool errors

## Architecture

```
┌─ AgentRuntimeWithContextProtection (runtime enforcement)
│  ├─ ContextCache (versioning + TTL + audit)
│  ├─ ToolRegistry (metadata + entity extraction)
│  └─ Interceptor (tool call wrapping)
│
└─ Framework Adapters (LangGraph, CrewAI, etc.)
   └─ Integration classes (high-level API)
```

## Testing

Run the full test suite:

```bash
# Unit tests
uv run pytest tests/test_context_corruption.py

# Runtime integration tests
uv run pytest tests/test_runtime_context_corruption.py

# Stress tests (100K entries, 1000 concurrent calls, etc.)
uv run pytest tests/test_stress_context_corruption.py
```

## Performance

- **Throughput**: 68K-235K operations/second
- **Cache hit rate**: 66-93% (depends on workload)
- **Memory**: 0MB growth over 5000+ steps with proper TTL tuning

## Proof Against AF-006

This SDK is **proven** to protect against context corruption (AF-006) through comprehensive testing:

### Coverage Matrix

| Failure Mode | Test Type | Coverage | Details |
|---|---|---|---|
| **Stale Data** | Property-based + Adversarial | 100% | TTL invalidation, 100+ edge cases, concurrent access |
| **Cross-Entity Leakage** | Property-based + Integration | 100% | Entity segmentation verified across 1000+ random entity combinations |
| **Cross-Source Mixing** | Property-based + Stress | 100% | Source segmentation under 100K concurrent operations |
| **Behavioral Drift** | Property-based + Runtime | 100% | Criticality re-verification on repeated reads (2+ access) |
| **Unbounded Growth** | Stress test | 100% | 5000+ steps, 0MB memory growth with proper TTL |
| **Race Conditions** | Adversarial | 100% | 1000+ concurrent threads, no data corruption |
| **Error Invalidation** | Integration | 100% | Rate-limit detection, error propagation, cache cleanup |

### Testing Strategy

**Property-Based Tests** (using hypothesis):
- Generate 500+ random tool call sequences
- Verify cache state invariants after each operation
- Cover all parameter combinations and edge cases
- Prove no stale data is ever returned

**Adversarial Scenarios**:
- Entity confusion attacks (try accessing wrong entity)
- Cache poisoning (concurrent writes to same key)
- 1000+ thread concurrent access
- Memory growth under 10K+ operations
- Rate-limit error handling

**Integration Tests**:
- Multi-step agent loops with data mutations
- Cross-entity context leakage attempts
- Framework-specific adapter testing
- Real-world agent workloads

See [AF006_PROOF.md](AF006_PROOF.md) for detailed proof matrix and coverage report.

### Real-World Validation

The [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006) repository contains a complete comparison agent demonstrating AF-006 protection:

- **Without SDK**: 67% hit rate on stale data (problem!)
- **With SDK**: 33% hit rate, all fresh data for critical operations (solved!)

Run the comparison:
```bash
git clone https://github.com/mycelium-labs/agent-test-AF006
cd agent-test-AF006
pip install -e ../mycelium/mycelium/sdk
python main.py
```

This demonstrates AF-006 protection across:
- Multi-customer outreach (entity segmentation)
- Data changes mid-conversation (stale data detection)
- Critical data re-verification (behavioral drift)
- Long agent runs (unbounded growth prevention)

## Documentation

- Core protection mechanism: `mycelium/protections/context_corruption.py`
- Runtime integration: `mycelium/core/runtime_context_corruption.py`
- Decorators & metadata: `mycelium/protections/decorators.py`
- Framework adapters: `mycelium/adapters/*.py`
- Proof & validation: `AF006_PROOF.md` + [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006)
