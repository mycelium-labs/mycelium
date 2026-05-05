"""
Performance benchmark for the @protect decorator.

Measures throughput and latency of the primary API
across the access patterns most common in real agent runs.
"""

import asyncio
import sys
import time
from pathlib import Path

SDK_PATH = Path(__file__).parent.parent / "sdk"
if str(SDK_PATH) not in sys.path:
    sys.path.insert(0, str(SDK_PATH))

from mycelium import protect, Session


# --- Simulated tool functions ---

async def _noop(entity_id: str) -> dict:
    return {"entity_id": entity_id, "value": 42}

async def _noop_noarg() -> dict:
    return {"value": 99}


# --- Benchmark helpers ---

async def _run(label: str, coro_factory, n: int = 1000) -> None:
    t0 = time.perf_counter()
    for _ in range(n):
        await coro_factory()
    elapsed = time.perf_counter() - t0
    ops = n / elapsed
    print(f"  {label:<40} {ops:>10,.0f} ops/sec   ({elapsed*1000:.1f}ms total)")


# --- Scenarios ---

async def bench_cache_hits(n: int = 10_000) -> None:
    """All calls are cache hits after the first."""
    @protect(entity_param="entity_id", ttl=300)
    async def tool(entity_id: str) -> dict:
        return await _noop(entity_id=entity_id)

    async with Session():
        await tool(entity_id="e1")  # warm
        t0 = time.perf_counter()
        for _ in range(n):
            await tool(entity_id="e1")
        elapsed = time.perf_counter() - t0

    ops = n / elapsed
    print(f"  {'Cache hits (same entity)':<40} {ops:>10,.0f} ops/sec   ({elapsed*1000:.1f}ms total)")


async def bench_entity_churn(n: int = 5_000) -> None:
    """Each call is a different entity — all cache misses, pure overhead."""
    @protect(entity_param="entity_id", ttl=300)
    async def tool(entity_id: str) -> dict:
        return await _noop(entity_id=entity_id)

    async with Session():
        t0 = time.perf_counter()
        for i in range(n):
            await tool(entity_id=f"e{i}")
        elapsed = time.perf_counter() - t0

    ops = n / elapsed
    print(f"  {'Entity churn (all misses)':<40} {ops:>10,.0f} ops/sec   ({elapsed*1000:.1f}ms total)")


async def bench_mixed_entities(n_entities: int = 20, calls_per_entity: int = 50) -> None:
    """n_entities entities, each called calls_per_entity times."""
    @protect(entity_param="entity_id", ttl=300)
    async def tool(entity_id: str) -> dict:
        return await _noop(entity_id=entity_id)

    total = n_entities * calls_per_entity
    async with Session():
        t0 = time.perf_counter()
        for i in range(calls_per_entity):
            for j in range(n_entities):
                await tool(entity_id=f"e{j}")
        elapsed = time.perf_counter() - t0

    ops = total / elapsed
    print(f"  {'Mixed ({n_entities} entities × {calls_per_entity})':<40} {ops:>10,.0f} ops/sec   ({elapsed*1000:.1f}ms total)"
          .replace("{n_entities}", str(n_entities)).replace("{calls_per_entity}", str(calls_per_entity)))


async def bench_concurrent(n_tasks: int = 20, calls_per_task: int = 500) -> None:
    """Concurrent tasks, each hitting the cache on a shared entity."""
    @protect(entity_param="entity_id", ttl=300)
    async def tool(entity_id: str) -> dict:
        return await _noop(entity_id=entity_id)

    total = n_tasks * calls_per_task

    async def worker():
        for _ in range(calls_per_task):
            await tool(entity_id="shared")

    async with Session():
        await tool(entity_id="shared")  # warm
        t0 = time.perf_counter()
        await asyncio.gather(*[worker() for _ in range(n_tasks)])
        elapsed = time.perf_counter() - t0

    ops = total / elapsed
    print(f"  {'Concurrent ({n_tasks} tasks × {calls_per_task})':<40} {ops:>10,.0f} ops/sec   ({elapsed*1000:.1f}ms total)"
          .replace("{n_tasks}", str(n_tasks)).replace("{calls_per_task}", str(calls_per_task)))


async def bench_ttl_expiry(n: int = 1_000) -> None:
    """Every call is a cache miss due to zero TTL (worst case overhead)."""
    @protect(entity_param="entity_id", ttl=0)
    async def tool(entity_id: str) -> dict:
        return await _noop(entity_id=entity_id)

    async with Session():
        t0 = time.perf_counter()
        for _ in range(n):
            await tool(entity_id="e1")
        elapsed = time.perf_counter() - t0

    ops = n / elapsed
    print(f"  {'TTL=0 (always miss / worst case)':<40} {ops:>10,.0f} ops/sec   ({elapsed*1000:.1f}ms total)")


async def bench_no_entity_param(n: int = 10_000) -> None:
    """No entity_param — single cache key per tool."""
    @protect(ttl=300)
    async def tool() -> dict:
        return await _noop_noarg()

    async with Session():
        await tool()  # warm
        t0 = time.perf_counter()
        for _ in range(n):
            await tool()
        elapsed = time.perf_counter() - t0

    ops = n / elapsed
    print(f"  {'No entity_param (global key)':<40} {ops:>10,.0f} ops/sec   ({elapsed*1000:.1f}ms total)")


async def bench_session_overhead() -> None:
    """Overhead of Session context manager itself."""
    n = 10_000
    t0 = time.perf_counter()
    for _ in range(n):
        async with Session():
            pass
    elapsed = time.perf_counter() - t0
    ops = n / elapsed
    print(f"  {'Session create/destroy (empty)':<40} {ops:>10,.0f} ops/sec   ({elapsed*1000:.1f}ms total)")


async def main() -> None:
    print("@protect decorator benchmark")
    print("=" * 60)
    print()

    print("Cache hit throughput:")
    await bench_cache_hits()
    await bench_no_entity_param()
    print()

    print("Cache miss throughput (real function always called):")
    await bench_entity_churn()
    await bench_ttl_expiry()
    print()

    print("Mixed access patterns:")
    await bench_mixed_entities()
    await bench_concurrent()
    print()

    print("Infrastructure:")
    await bench_session_overhead()
    print()


if __name__ == "__main__":
    asyncio.run(main())
