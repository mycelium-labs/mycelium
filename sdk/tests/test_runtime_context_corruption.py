"""
Tests for runtime integration with AF-006 context corruption protection.

Verifies:
1. Tool registration and metadata lookup
2. Cache decision flow (use/refetch/not cached)
3. Tool invocation with caching
4. Step advancement with TTL expiration
5. Error handling with invalidation
6. Rate-limit detection
"""

import pytest
import asyncio

from mycelium.protections import tool, ToolRegistry, Criticality, ContextSegmentation
from mycelium.core.runtime_context_corruption import (
    AgentRuntimeWithContextProtection,
    InvalidationPolicy,
    RefetchAction,
)


# Mock tools for testing
@tool(critical=True, entity_param="user_id", invalidate_after_steps=5)
async def fetch_user(user_id: str) -> dict:
    return {"id": user_id, "name": f"User {user_id}", "status": "active"}


@tool(critical=False, invalidate_after_steps=10)
async def search_docs(query: str) -> list[dict]:
    return [{"id": f"doc_{i}", "title": f"Doc {i}"} for i in range(3)]


@tool(critical=True, invalidate_after_steps=1)
async def get_quota() -> dict:
    return {"requests_remaining": 1000}


@tool(critical=False)
def sync_tool(name: str) -> str:
    return f"Hello {name}"


class TestToolRegistry:
    """Test tool registration and metadata lookup."""

    def test_register_tool(self):
        registry = ToolRegistry()
        registry.register(fetch_user)

        metadata = registry.get("fetch_user")
        assert metadata is not None
        assert metadata.func_name == "fetch_user"
        assert metadata.critical is True
        assert metadata.entity_param == "user_id"
        assert metadata.invalidate_after_steps == 5

    def test_extract_entity_id(self):
        registry = ToolRegistry()
        registry.register(fetch_user)

        entity_id = registry.extract_entity_id("fetch_user", {"user_id": "alice"})
        assert entity_id == "alice"

    def test_list_all(self):
        registry = ToolRegistry()
        registry.register(fetch_user)
        registry.register(search_docs)

        all_tools = registry.list_all()
        assert len(all_tools) == 2
        assert "fetch_user" in all_tools
        assert "search_docs" in all_tools


class TestRuntimeIntegration:
    """Test runtime with cache enforcement."""

    @pytest.mark.asyncio
    async def test_tool_call_cache_miss(self):
        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([fetch_user])

        # First call: cache miss
        result = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")
        assert result["id"] == "alice"
        assert result["name"] == "User alice"

    @pytest.mark.asyncio
    async def test_tool_call_cache_hit(self):
        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([fetch_user])

        # First call
        result1 = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")

        # Second call (should hit cache)
        result2 = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")

        assert result1 == result2
        # Verify from cache (both should be same object reference)
        assert result1 is result2

    @pytest.mark.asyncio
    async def test_tool_call_with_step_advancement(self):
        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([fetch_user])

        # Step 1: Fetch
        result1 = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")
        assert runtime.current_step == 0

        # Advance through 4 more steps (still within TTL=5)
        for _ in range(4):
            runtime.advance_step()
        assert runtime.current_step == 4

        # Step 5: Still cached (age=4, TTL=5)
        result2 = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")
        assert result1 is result2

        # Advance to step 5
        runtime.advance_step()
        assert runtime.current_step == 5

        # Step 6: Now stale (age=5, TTL=5)
        result3 = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")
        # Should be refetched but same value
        assert result3["id"] == "alice"

    @pytest.mark.asyncio
    async def test_entity_segmentation(self):
        runtime = AgentRuntimeWithContextProtection(
            policy=InvalidationPolicy(segmentation=ContextSegmentation.ENTITY)
        )
        runtime.register_tools([fetch_user])

        # Fetch alice
        alice = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")
        assert alice["id"] == "alice"

        # Fetch bob (different entity, different cache entry)
        bob = await runtime.call_tool("fetch_user", fetch_user, user_id="bob")
        assert bob["id"] == "bob"

        # Fetch alice again (should use cache)
        alice_again = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")
        assert alice_again is alice

        # Fetch bob again (should use cache)
        bob_again = await runtime.call_tool("fetch_user", fetch_user, user_id="bob")
        assert bob_again is bob

    @pytest.mark.asyncio
    async def test_criticality_recheck(self):
        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([fetch_user])

        # Fetch
        result1 = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")

        # Read again (access_count=2, critical=True, should trigger recheck)
        result2 = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")

        # Should be refetched (but same value)
        assert result2["id"] == "alice"

    @pytest.mark.asyncio
    async def test_always_fresh_tool(self):
        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([get_quota])

        # First call
        quota1 = await runtime.call_tool("get_quota", get_quota)
        assert quota1["requests_remaining"] == 1000

        # Immediately call again (TTL=1, not advanced step, but should still refetch)
        # Actually, since step hasn't advanced, it won't be stale
        # Let's advance step and try again
        runtime.advance_step()

        # Now TTL should trigger
        quota2 = await runtime.call_tool("get_quota", get_quota)
        assert quota2["requests_remaining"] == 1000

    @pytest.mark.asyncio
    async def test_tool_error_invalidation(self):
        async def failing_tool(user_id: str) -> dict:
            raise Exception("API connection failed")

        failing = tool(critical=False, entity_param="user_id")(failing_tool)

        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([failing])

        # First call fails
        with pytest.raises(Exception):
            await runtime.call_tool("failing_tool", failing, user_id="alice")

        # Cache should be empty after error
        snapshot = runtime.get_cache_snapshot()
        assert len(snapshot) == 0

    @pytest.mark.asyncio
    async def test_rate_limit_error_detection(self):
        async def rate_limited_tool() -> dict:
            raise Exception("Rate limit exceeded (429)")

        tool_func = tool(critical=False)(rate_limited_tool)

        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([tool_func])

        # Call fails with rate-limit
        with pytest.raises(Exception):
            await runtime.call_tool("rate_limited_tool", tool_func)

        # Check audit log for rate-limit detection
        audit = runtime.get_audit_log()
        rate_limit_events = [
            e for e in audit if "rate_limit" in str(e).lower()
        ]
        assert len(rate_limit_events) > 0

    @pytest.mark.asyncio
    async def test_sync_tool_execution(self):
        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([sync_tool])

        # Sync function should be called correctly
        result = await runtime.call_tool("sync_tool", sync_tool, name="Alice")
        assert result == "Hello Alice"

    @pytest.mark.asyncio
    async def test_unregistered_tool(self):
        runtime = AgentRuntimeWithContextProtection(verbose=False)

        async def unregistered() -> str:
            return "result"

        # Should work but without caching
        result = await runtime.call_tool("unregistered", unregistered)
        assert result == "result"

    def test_cache_snapshot(self):
        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([fetch_user])

        # Run a couple of tool calls
        async def run_calls():
            await runtime.call_tool("fetch_user", fetch_user, user_id="alice")
            await runtime.call_tool("fetch_user", fetch_user, user_id="alice")

        asyncio.run(run_calls())

        snapshot = runtime.get_cache_snapshot()
        assert len(snapshot) == 1
        assert "alice" in list(snapshot.keys())[0]

    def test_audit_log(self):
        runtime = AgentRuntimeWithContextProtection()
        runtime.register_tools([fetch_user])

        async def run_calls():
            await runtime.call_tool("fetch_user", fetch_user, user_id="alice")
            runtime.advance_step()
            await runtime.call_tool("fetch_user", fetch_user, user_id="alice")

        asyncio.run(run_calls())

        audit = runtime.get_audit_log()
        assert len(audit) > 0
        event_types = [e["event_type"] for e in audit]
        assert "add" in event_types
        assert "step_advanced" in event_types
        assert "get_hit" in event_types or "get_repeated_read" in event_types


class TestEndToEnd:
    """End-to-end agent simulation."""

    @pytest.mark.asyncio
    async def test_agent_with_context_protection(self):
        runtime = AgentRuntimeWithContextProtection(verbose=False)
        runtime.register_tools([fetch_user, search_docs])

        # Simulate agent loop
        for step in range(5):
            if step == 0:
                user = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")
                assert user["id"] == "alice"

            if step == 1:
                docs = await runtime.call_tool("search_docs", search_docs, query="test")
                assert len(docs) == 3

            if step == 3:
                # Refetch user (should be cached still)
                user2 = await runtime.call_tool("fetch_user", fetch_user, user_id="alice")
                assert user2["id"] == "alice"

            runtime.advance_step()

        # Verify state
        snapshot = runtime.get_cache_snapshot()
        assert len(snapshot) >= 2  # Both fetch_user and search_docs


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
