"""
Stress Tests for AF-006 Context Corruption Protection

Tests ContextCache under extreme load:
- Concurrent tool calls (100+ simultaneous)
- Large context (10K+ entries)
- Long agent runs (1000+ steps)
- Rapid entity churn (1000+ entities)
- Memory pressure
- Cache hit rate distribution
- Invalidation throughput
"""

import asyncio
import time
import psutil
import pytest
from typing import List
from mycelium.protections import tool, Criticality, ContextSegmentation
from mycelium.core.runtime_context_corruption import (
    AgentRuntimeWithContextProtection,
    InvalidationPolicy,
)


# Mock tools for stress testing
@tool(critical=False, invalidate_after_steps=10)
async def fetch_data(entity_id: str, index: int) -> dict:
    """Fetch data for stress testing."""
    return {"entity_id": entity_id, "index": index, "data": f"data_{index}"}


@tool(critical=True, entity_param="entity_id", invalidate_after_steps=5)
async def get_entity_state(entity_id: str) -> dict:
    """Get entity state (critical)."""
    return {"entity_id": entity_id, "state": "active", "timestamp": time.time()}


@tool(critical=False, invalidate_after_steps=1)
async def always_fresh() -> dict:
    """Tool that's always fresh."""
    return {"timestamp": time.time()}


class TestConcurrentAccess:
    """Test concurrent tool calls."""

    @pytest.mark.asyncio
    async def test_100_concurrent_calls(self):
        """100 concurrent tool calls, should not deadlock or corrupt state."""
        runtime = AgentRuntimeWithContextProtection(verbose=False)
        runtime.register_tools([fetch_data])

        async def make_call(index: int):
            return await runtime.call_tool(
                "fetch_data", fetch_data, entity_id=f"entity_{index % 10}", index=index
            )

        # Launch 100 concurrent calls
        tasks = [make_call(i) for i in range(100)]
        results = await asyncio.gather(*tasks)

        # Verify all completed successfully
        assert len(results) == 100
        assert all(r is not None for r in results)

    @pytest.mark.asyncio
    async def test_1000_concurrent_calls(self):
        """1000 concurrent calls, stress test cache under load."""
        runtime = AgentRuntimeWithContextProtection(verbose=False)
        runtime.register_tools([fetch_data])

        async def make_call(index: int):
            return await runtime.call_tool(
                "fetch_data", fetch_data, entity_id=f"entity_{index % 50}", index=index
            )

        # Launch 1000 concurrent calls
        tasks = [make_call(i) for i in range(1000)]
        start = time.time()
        results = await asyncio.gather(*tasks)
        elapsed = time.time() - start

        assert len(results) == 1000
        assert all(r is not None for r in results)
        print(f"\n1000 concurrent calls completed in {elapsed:.2f}s ({1000/elapsed:.0f} ops/sec)")


class TestLargeContext:
    """Test with massive context."""

    @pytest.mark.asyncio
    async def test_10k_cache_entries(self):
        """10,000 cache entries, measure memory and lookup time."""
        runtime = AgentRuntimeWithContextProtection(verbose=False)
        runtime.register_tools([fetch_data])

        # Fill cache with 10K entries (different entities)
        for i in range(10_000):
            entity_id = f"entity_{i}"
            await runtime.call_tool(
                "fetch_data", fetch_data, entity_id=entity_id, index=i
            )

        # Measure memory
        process = psutil.Process()
        mem_before = process.memory_info().rss / 1024 / 1024  # MB

        # Measure lookup time (access random entries)
        start = time.time()
        for i in range(100):
            entity_id = f"entity_{i * 100}"
            result = await runtime.call_tool(
                "fetch_data", fetch_data, entity_id=entity_id, index=0
            )
            assert result is not None

        lookup_time = time.time() - start
        mem_after = process.memory_info().rss / 1024 / 1024  # MB

        print(
            f"\n10K entries: {mem_before:.0f}MB → {mem_after:.0f}MB "
            f"(+{mem_after - mem_before:.0f}MB), lookup time: {lookup_time*1000:.1f}ms for 100 accesses"
        )

    @pytest.mark.asyncio
    async def test_100k_cache_entries(self):
        """100,000 cache entries (extreme case)."""
        runtime = AgentRuntimeWithContextProtection(
            policy=InvalidationPolicy(segmentation=ContextSegmentation.ENTITY),
            verbose=False,
        )
        runtime.register_tools([get_entity_state])

        # Fill cache with 100K entries (different entities)
        print("\nFilling cache with 100K entries...")
        for i in range(100_000):
            entity_id = f"entity_{i}"
            await runtime.call_tool(
                "get_entity_state", get_entity_state, entity_id=entity_id
            )
            if (i + 1) % 10_000 == 0:
                print(f"  {i + 1:,} entries cached")

        # Measure memory
        process = psutil.Process()
        mem = process.memory_info().rss / 1024 / 1024

        # Snapshot
        snapshot = runtime.get_cache_snapshot()
        print(f"Memory: {mem:.0f}MB, Cache entries: {len(snapshot):,}")

        assert len(snapshot) == 100_000


class TestLongRunningAgent:
    """Test agent running for 1000+ steps."""

    @pytest.mark.asyncio
    async def test_1000_step_run(self):
        """Agent runs for 1000 steps."""
        runtime = AgentRuntimeWithContextProtection(verbose=False)
        runtime.register_tools([fetch_data, get_entity_state])

        # Simulate 1000-step agent run
        start = time.time()
        for step in range(1000):
            # Call tools
            if step % 100 == 0:
                result = await runtime.call_tool(
                    "get_entity_state", get_entity_state, entity_id="entity_0"
                )
            result = await runtime.call_tool(
                "fetch_data", fetch_data, entity_id="entity_0", index=step
            )
            runtime.advance_step()

        elapsed = time.time() - start
        print(f"\n1000 steps completed in {elapsed:.2f}s ({1000/elapsed:.0f} steps/sec)")


class TestRapidEntityChurn:
    """Test with many different entities."""

    @pytest.mark.asyncio
    async def test_1000_entities(self):
        """Cache for 1000 different entities."""
        runtime = AgentRuntimeWithContextProtection(
            policy=InvalidationPolicy(segmentation=ContextSegmentation.ENTITY),
            verbose=False,
        )
        runtime.register_tools([get_entity_state])

        # Create 1000 entities, each with state
        for i in range(1000):
            entity_id = f"entity_{i}"
            await runtime.call_tool(
                "get_entity_state", get_entity_state, entity_id=entity_id
            )

        # Access them randomly
        import random

        for _ in range(1000):
            entity_id = f"entity_{random.randint(0, 999)}"
            result = await runtime.call_tool(
                "get_entity_state", get_entity_state, entity_id=entity_id
            )
            assert result is not None

        snapshot = runtime.get_cache_snapshot()
        print(f"\n1000 entities, {len(snapshot)} cache entries")


class TestMemoryPressure:
    """Test memory usage under load."""

    @pytest.mark.asyncio
    async def test_memory_growth(self):
        """Monitor memory growth over time."""
        runtime = AgentRuntimeWithContextProtection(verbose=False)
        runtime.register_tools([fetch_data, always_fresh])

        process = psutil.Process()
        mem_samples = []

        # 5000 steps, monitoring memory
        for step in range(5000):
            entity_id = f"entity_{step % 100}"
            await runtime.call_tool(
                "fetch_data", fetch_data, entity_id=entity_id, index=step
            )
            runtime.advance_step()

            # Sample memory every 500 steps
            if step % 500 == 0:
                mem = process.memory_info().rss / 1024 / 1024
                mem_samples.append(mem)

        print(f"\nMemory growth over 5000 steps:")
        for i, mem in enumerate(mem_samples):
            print(f"  Step {i*500}: {mem:.0f}MB")

        # Check if memory growth is reasonable
        if len(mem_samples) > 1:
            growth = mem_samples[-1] - mem_samples[0]
            print(f"Total growth: {growth:.0f}MB")
            # Should not grow unboundedly
            assert growth < 500  # Less than 500MB growth


class TestCacheHitRate:
    """Measure cache hit rate under different workloads."""

    @pytest.mark.asyncio
    async def test_hit_rate_sequential(self):
        """Sequential access, high hit rate expected."""
        runtime = AgentRuntimeWithContextProtection(
            policy=InvalidationPolicy(default_ttl_steps=20), verbose=False
        )
        runtime.register_tools([fetch_data])

        # Access same entity repeatedly
        for step in range(100):
            await runtime.call_tool(
                "fetch_data", fetch_data, entity_id="entity_0", index=0
            )
            runtime.advance_step()

        # Count cache hits in audit log
        audit = runtime.get_audit_log()
        hits = len([e for e in audit if e["event_type"] == "get_hit"])
        total_gets = len([e for e in audit if "get_" in e["event_type"]])

        hit_rate = hits / total_gets if total_gets > 0 else 0
        print(f"\nSequential access: {hit_rate*100:.1f}% hit rate ({hits}/{total_gets})")

    @pytest.mark.asyncio
    async def test_hit_rate_random(self):
        """Random entity access, lower hit rate expected."""
        import random

        runtime = AgentRuntimeWithContextProtection(
            policy=InvalidationPolicy(default_ttl_steps=5), verbose=False
        )
        runtime.register_tools([fetch_data])

        # Random entity access
        for step in range(100):
            entity_id = f"entity_{random.randint(0, 20)}"
            await runtime.call_tool(
                "fetch_data", fetch_data, entity_id=entity_id, index=step
            )
            runtime.advance_step()

        # Count cache hits
        audit = runtime.get_audit_log()
        hits = len([e for e in audit if e["event_type"] == "get_hit"])
        total_gets = len([e for e in audit if "get_" in e["event_type"]])

        hit_rate = hits / total_gets if total_gets > 0 else 0
        print(f"\nRandom access: {hit_rate*100:.1f}% hit rate ({hits}/{total_gets})")


class TestInvalidationThroughput:
    """Test invalidation performance."""

    @pytest.mark.asyncio
    async def test_invalidate_10k_entries(self):
        """Invalidate 10,000 entries at once."""
        runtime = AgentRuntimeWithContextProtection(verbose=False)
        runtime.register_tools([fetch_data])

        # Fill cache with 10K entries
        for i in range(10_000):
            runtime.cache.add(
                name=f"entry_{i}",
                value={"data": i},
                source="test_source",
                entity_id=f"entity_{i % 100}",
            )

        # Measure time to invalidate all entries from one source
        start = time.time()
        for i in range(10_000):
            runtime.cache.invalidate_on_error(
                source="test_source",
                error=Exception("test"),
                entity_id=f"entity_{i % 100}",
            )
        elapsed = time.time() - start

        print(f"\nInvalidate 10K entries: {elapsed:.3f}s ({10_000/elapsed:.0f} ops/sec)")


class TestCorrectnesUnderStress:
    """Verify correctness even under stress."""

    @pytest.mark.asyncio
    async def test_no_cross_entity_leakage_under_stress(self):
        """Ensure cross-entity isolation even with concurrent access."""
        runtime = AgentRuntimeWithContextProtection(
            policy=InvalidationPolicy(segmentation=ContextSegmentation.ENTITY),
            verbose=False,
        )
        runtime.register_tools([get_entity_state])

        async def entity_thread(entity_id: str):
            for step in range(100):
                result = await runtime.call_tool(
                    "get_entity_state", get_entity_state, entity_id=entity_id
                )
                assert result["entity_id"] == entity_id
                runtime.advance_step()

        # Run 10 entities concurrently
        await asyncio.gather(*[entity_thread(f"entity_{i}") for i in range(10)])

        # Verify no cross-entity leakage
        snapshot = runtime.get_cache_snapshot()
        for key, entry in snapshot.items():
            # Key should contain entity_id from the value
            assert entry["value"]["entity_id"] in key

    @pytest.mark.asyncio
    async def test_versioning_correctness_under_stress(self):
        """Ensure versioning is correct under stress."""
        runtime = AgentRuntimeWithContextProtection(verbose=False)
        runtime.register_tools([fetch_data])

        # Repeatedly update the same entry
        entity_id = "entity_0"
        versions = []

        for step in range(100):
            version = runtime.cache.add(
                name="test_entry",
                value={"step": step},
                source="fetch_data",
                entity_id=entity_id,
            )
            versions.append(version)
            runtime.advance_step()

        # All versions should be unique
        assert len(set(versions)) == len(versions), "Duplicate versions detected!"

        # Verify history
        history = runtime.cache.get_history("test_entry", "fetch_data", entity_id)
        assert len(history.versions) == 100


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
