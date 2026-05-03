"""
Example: Using context corruption protection in your agent.

This shows:
1. How developers mark tools with @tool decorator
2. How the runtime calls tools with cache enforcement
3. How to interpret cache decisions
4. Best practices for avoiding context corruption
"""

from mycelium.protections.decorators import tool, protect
from mycelium.protections.context_corruption import (
    ContextCache,
    InvalidationPolicy,
    ContextSegmentation,
    Criticality,
)
import asyncio


# ============================================================================
# STEP 1: Developer marks tools with context corruption rules
# ============================================================================


@tool(
    critical=True,
    entity_param="user_id",
    invalidate_after_steps=5,
)
def fetch_user_profile(user_id: str) -> dict:
    """
    Fetch a user's profile data.

    Marked as CRITICAL because:
    - User ID is a resource handle; if stale, agent might act on wrong user
    - High cost to re-fetch, but re-verification is cheap

    Marked with ENTITY SEGMENTATION because:
    - Data for user_123 must never be confused with data for user_456

    Invalidates every 5 steps because:
    - User data changes infrequently
    - But we want reasonable freshness for permission/status checks
    """
    return {
        "id": user_id,
        "name": f"User {user_id}",
        "status": "active",
        "permissions": ["read", "write"],
    }


@tool(
    critical=False,
    invalidate_after_steps=10,
)
def search_documents(query: str, max_results: int = 5) -> list[dict]:
    """
    Search for documents matching a query.

    Not marked as CRITICAL because:
    - Search results are advisory, not action triggers
    - If stale, worst case is slightly outdated recommendation

    Global scope (no entity_param) because:
    - Same query always returns same results
    - No cross-entity segmentation needed
    """
    return [
        {"id": f"doc_{i}", "title": f"Doc {i}", "relevance": 0.9 - i * 0.1}
        for i in range(max_results)
    ]


@tool(
    critical=True,
    invalidate_after_steps=1,
)
def get_api_quota() -> dict:
    """
    Check remaining API quota.

    Marked as CRITICAL and ALWAYS-FRESH (invalidate_after_steps=1) because:
    - Quota can change between tool calls
    - Agent might exhaust quota if using stale data
    - High cost of exceeding quota (rate-limited)

    Global scope because quota is system-wide, not per-entity.
    """
    return {
        "requests_remaining": 1000,
        "requests_per_minute": 100,
        "reset_at": "2026-05-03T12:00:00Z",
    }


# ============================================================================
# STEP 2: Runtime manages cache and enforces invalidation
# ============================================================================


class SimpleAgentRuntime:
    """
    Simplified agent runtime showing how to use ContextCache.

    In production, this logic lives in the core runtime and is transparent
    to the developer. But here we show it explicitly for understanding.
    """

    def __init__(self):
        policy = InvalidationPolicy(
            default_ttl_steps=5,
            criticality_recheck_threshold=2,
            segmentation=ContextSegmentation.BOTH,
        )
        self.cache = ContextCache(policy)
        self.step = 0

    async def call_tool(
        self, tool_name: str, tool_func, tool_kwargs: dict
    ) -> tuple[dict, str]:
        """
        Call a tool with cache enforcement.

        This is what the runtime does behind the scenes:
        1. Check if result is in cache
        2. If in cache and fresh, return cached value
        3. If in cache but stale/critical+repeated, refetch
        4. If not in cache, fetch
        5. Store in cache with metadata
        6. Handle errors appropriately

        Returns: (result, decision_reason)
        """
        # Extract tool metadata
        metadata = tool_func._mycelium_tool_metadata
        entity_id = None
        if metadata.entity_param:
            entity_id = tool_kwargs.get(metadata.entity_param)

        # Try cache first
        decision = self.cache.get(
            name=tool_name,
            source=tool_name,
            entity_id=entity_id,
        )

        # If cache says "use cached value", use it
        if not decision.should_refetch:
            print(
                f"[CACHE HIT] {tool_name}(entity={entity_id}) "
                f"age={decision.age_steps} steps, "
                f"access_count={decision.access_count}, "
                f"reason: {decision.reason}"
            )
            return decision.value, f"cache_hit ({decision.reason})"

        # Otherwise, call the tool
        print(
            f"[CACHE MISS/REFETCH] {tool_name}(entity={entity_id}) "
            f"reason: {decision.reason}"
        )
        try:
            result = tool_func(**tool_kwargs)
        except Exception as e:
            # On error, invalidate related context
            is_rate_limit = self.cache.invalidate_on_error(
                source=tool_name, error=e, entity_id=entity_id
            )
            print(f"[ERROR] {tool_name} failed: {e} (rate_limit={is_rate_limit})")
            raise

        # Store in cache with metadata
        version_id = self.cache.add(
            name=tool_name,
            value=result,
            source=tool_name,
            entity_id=entity_id,
            criticality=Criticality.HIGH if metadata.critical else Criticality.LOW,
            invalidate_after_steps=metadata.invalidate_after_steps,
        )

        return result, f"fetched (version={version_id})"

    def advance_step(self):
        """Call this when agent completes one reasoning step."""
        self.step += 1
        self.cache.advance_step()
        print(f"\n--- STEP {self.step} ---")


# ============================================================================
# STEP 3: Example agent using these tools
# ============================================================================


async def example_agent():
    """Example: An agent that fetches user data and searches documents."""
    runtime = SimpleAgentRuntime()

    # Step 1: Fetch user profile
    print("\n=== Step 1: Fetch user profile ===")
    user_result, reason = await runtime.call_tool(
        "fetch_user_profile",
        fetch_user_profile,
        {"user_id": "alice"},
    )
    print(f"User: {user_result['name']} (status: {user_result['status']})")
    print(f"Reason: {reason}")
    runtime.advance_step()

    # Step 2: Search documents
    print("\n=== Step 2: Search documents ===")
    docs_result, reason = await runtime.call_tool(
        "search_documents",
        search_documents,
        {"query": "machine learning", "max_results": 3},
    )
    print(f"Found {len(docs_result)} documents")
    print(f"Reason: {reason}")
    runtime.advance_step()

    # Step 3: Check quota
    print("\n=== Step 3: Check API quota ===")
    quota_result, reason = await runtime.call_tool(
        "get_api_quota",
        get_api_quota,
        {},
    )
    print(f"Quota: {quota_result['requests_remaining']} requests remaining")
    print(f"Reason: {reason}")
    runtime.advance_step()

    # Step 4: Fetch user again (within 5 steps, should hit cache)
    print("\n=== Step 4: Fetch user again (should be cached) ===")
    user_result2, reason = await runtime.call_tool(
        "fetch_user_profile",
        fetch_user_profile,
        {"user_id": "alice"},
    )
    print(f"User: {user_result2['name']}")
    print(f"Reason: {reason}")
    runtime.advance_step()

    # Step 5: Search documents again (should still be cached)
    print("\n=== Step 5: Search documents again (should be cached) ===")
    docs_result2, reason = await runtime.call_tool(
        "search_documents",
        search_documents,
        {"query": "machine learning", "max_results": 3},
    )
    print(f"Found {len(docs_result2)} documents")
    print(f"Reason: {reason}")
    runtime.advance_step()

    # Step 6: Check quota again (ALWAYS FRESH, should refetch)
    print("\n=== Step 6: Check quota again (always fresh, should refetch) ===")
    quota_result2, reason = await runtime.call_tool(
        "get_api_quota",
        get_api_quota,
        {},
    )
    print(f"Quota: {quota_result2['requests_remaining']} requests remaining")
    print(f"Reason: {reason}")
    runtime.advance_step()

    # Step 7-10: More operations, user cache expires at step 6 (5 step TTL from step 1)
    for i in range(4):
        print(f"\n=== Step {7 + i}: Idle step ===")
        runtime.advance_step()

    # Step 11: Fetch user again (cache is stale now, should refetch)
    print("\n=== Step 11: Fetch user again (cache expired, should refetch) ===")
    user_result3, reason = await runtime.call_tool(
        "fetch_user_profile",
        fetch_user_profile,
        {"user_id": "alice"},
    )
    print(f"User: {user_result3['name']}")
    print(f"Reason: {reason}")
    runtime.advance_step()

    # Print audit log
    print("\n\n=== AUDIT LOG ===")
    for event in runtime.cache.get_audit_log():
        print(f"{event['event_type']:20} | step {event['step']:2} | {event['data']}")

    # Print state snapshot
    print("\n\n=== CACHE STATE SNAPSHOT ===")
    snapshot = runtime.cache.get_state_snapshot()
    for key, entry in snapshot.items():
        print(f"{key:40} | age={entry['age_steps']:2} | access={entry['access_count']} | criticality={entry['criticality']}")


if __name__ == "__main__":
    asyncio.run(example_agent())
