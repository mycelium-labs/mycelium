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

This SDK is **proven** to protect against context corruption (AF-006) through comprehensive testing and formal verification.

### Quick Proof Overview

| Failure Mode | Test Type | Coverage | Status |
|---|---|---|---|
| **Stale Data** | Property-based + Adversarial | 100% | ✅ Proven |
| **Cross-Entity Leakage** | Property-based + Integration | 100% | ✅ Proven |
| **Cross-Source Mixing** | Property-based + Stress | 100% | ✅ Proven |
| **Behavioral Drift** | Property-based + Runtime | 100% | ✅ Proven |
| **Unbounded Growth** | Stress test | 100% | ✅ Proven |
| **Race Conditions** | Adversarial | 100% | ✅ Proven |
| **Error Invalidation** | Integration | 100% | ✅ Proven |

**Total Coverage**: 47 direct + 500+ property-based + 12 adversarial = **600+ test cases**

### Proof Documentation

- **[PROOF_SUMMARY.md](PROOF_SUMMARY.md)** — Complete end-to-end proof across all 7 failure modes with formal invariants
- **[AF006_PROOF.md](AF006_PROOF.md)** — Detailed test matrix, coverage report, and test execution guide
- **[agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006)** — Real-world comparison agent demonstrating practical protection

### Run the Proof

```bash
# Run all 600+ test cases
uv run pytest tests/ -v

# Run comparison agent (real-world validation)
cd ../agent-test-AF006
python main.py
```

### Real-World Results

The [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006) comparison agent shows practical protection:

**Without Mycelium SDK**:
- Cache hit rate: 67% (misleadingly high)
- Data freshness: ⚠️ STALE (outdated customer profiles)
- Entity isolation: ❌ RISK (no explicit segmentation)
- Critical re-verify: ❌ NEVER (cache forever)
- Memory growth: 📈 UNBOUNDED

**With Mycelium SDK**:
- Cache hit rate: 33% (balanced, forced refetches for freshness)
- Data freshness: ✅ GUARANTEED (TTL enforcement)
- Entity isolation: ✅ ENFORCED (per-entity cache keys)
- Critical re-verify: ✅ AUTOMATIC (threshold-based refetch)
- Memory growth: ✅ BOUNDED (0MB growth over 5000 steps)

### Test Scenarios

The comparison agent validates AF-006 protection across 4 real-world scenarios:

1. **Multi-Customer Outreach** — Entity segmentation prevents cross-customer leakage
2. **Data Changes Mid-Conversation** — TTL enforcement detects stale data
3. **Critical Data Re-Verification** — Repeated reads trigger refetch on critical tools
4. **Long Agent Run** — Memory stays bounded with TTL cleanup

## Documentation

**Code**:
- Core protection mechanism: `mycelium/protections/context_corruption.py`
- Runtime integration: `mycelium/core/runtime_context_corruption.py`
- Decorators & metadata: `mycelium/protections/decorators.py`
- Framework adapters: `mycelium/adapters/*.py`

**Proof & Validation**:
- [PROOF_SUMMARY.md](PROOF_SUMMARY.md) — End-to-end proof across all 7 failure modes
- [AF006_PROOF.md](AF006_PROOF.md) — Detailed test matrix and invariant proofs
- [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006) — Real-world comparison agent
- [TESTING.md](https://github.com/mycelium-labs/agent-test-AF006/blob/main/TESTING.md) — How to run all tests

**Getting Started**:
- [README.md](README.md) (this file) — Quick start and usage examples
- [Installation](#installation) — Install as library
- [Usage](#usage) — Framework integrations and examples
