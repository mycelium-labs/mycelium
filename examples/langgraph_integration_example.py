"""
LangGraph Integration Example

Shows how to use Mycelium AF-006 protection with LangGraph agents.
"""

import asyncio
from typing import TypedDict
from mycelium.protections import tool
from mycelium.adapters.langgraph import LangGraphIntegration


# Define state
class AgentState(TypedDict):
    user_id: str
    query: str
    context: dict
    result: str


# Define tools
@tool(critical=True, entity_param="user_id", invalidate_after_steps=5)
async def fetch_user_profile(user_id: str) -> dict:
    """Fetch user profile (critical, entity-scoped)."""
    print(f"  → Fetching profile for {user_id}")
    return {
        "user_id": user_id,
        "name": f"User {user_id}",
        "preferences": {"theme": "dark"},
    }


@tool(critical=False, invalidate_after_steps=10)
async def search_knowledge_base(query: str) -> list:
    """Search knowledge base (non-critical)."""
    print(f"  → Searching for: {query}")
    return [
        {"id": "doc_1", "title": "Doc 1", "relevance": 0.95},
        {"id": "doc_2", "title": "Doc 2", "relevance": 0.87},
    ]


@tool(critical=True, invalidate_after_steps=1)
async def check_rate_limit() -> dict:
    """Check API rate limit (always fresh)."""
    print(f"  → Checking rate limit")
    return {"requests_remaining": 1000, "limit_per_minute": 100}


# LangGraph-style node functions
async def fetch_context_node(state: AgentState, protection) -> dict:
    """Fetch user context and knowledge."""
    print("\n[NODE] fetch_context")
    print(f"  User: {state['user_id']}, Query: {state['query']}")

    # Call tools through protection
    profile = await protection.call_tool(
        "fetch_user_profile", fetch_user_profile, user_id=state["user_id"]
    )
    docs = await protection.call_tool(
        "search_knowledge_base", search_knowledge_base, query=state["query"]
    )

    return {
        "context": {"profile": profile, "docs": docs},
    }


async def check_limits_node(state: AgentState, protection) -> dict:
    """Check API limits."""
    print("\n[NODE] check_limits")

    # Always-fresh check
    limit = await protection.call_tool(
        "check_rate_limit", check_rate_limit
    )

    if limit["requests_remaining"] < 100:
        print("  ⚠️  Running low on quota")

    return {}


async def respond_node(state: AgentState, protection) -> dict:
    """Generate response."""
    print("\n[NODE] respond")
    print(f"  Generating response based on context")

    # Access context again (should hit cache)
    profile = await protection.call_tool(
        "fetch_user_profile", fetch_user_profile, user_id=state["user_id"]
    )

    return {
        "result": f"Hello {profile['name']}, answering: {state['query']}"
    }


async def main():
    """Simulate LangGraph agent with protection."""
    print("=" * 70)
    print("LangGraph Integration Example")
    print("=" * 70)

    # Initialize integration
    integration = LangGraphIntegration(verbose=True)
    integration.register_tools(
        {
            "fetch_user_profile": fetch_user_profile,
            "search_knowledge_base": search_knowledge_base,
            "check_rate_limit": check_rate_limit,
        },
        critical_tools=["fetch_user_profile", "check_rate_limit"],
    )

    protection = integration.get_protection()

    # Simulate agent state
    state = AgentState(
        user_id="alice",
        query="How do I use this system?",
        context={},
        result="",
    )

    # Execute nodes
    print("\n[AGENT] Starting agent run\n")

    state.update(await fetch_context_node(state, protection))
    protection.advance_step()

    state.update(await check_limits_node(state, protection))
    protection.advance_step()

    state.update(await respond_node(state, protection))
    protection.advance_step()

    # More steps (to trigger stale data)
    print("\n[NODE] idle steps (2-4)")
    for _ in range(3):
        protection.advance_step()
        print(f"  Step advanced")

    # Access profile again (should refetch due to TTL)
    print("\n[NODE] fetch_context_again (after TTL expired)")
    profile = await protection.call_tool(
        "fetch_user_profile", fetch_user_profile, user_id=state["user_id"]
    )
    protection.advance_step()

    print(f"\nFinal result: {state['result']}")

    # Show stats
    stats = integration.get_stats()
    print("\n" + "=" * 70)
    print("PROTECTION STATS")
    print("=" * 70)
    print(f"Cache entries: {stats['cache_entries']}")
    print(f"Cache hits: {stats['cache_hits']}")
    print(f"Cache misses: {stats['cache_misses']}")
    print(f"Hit rate: {stats['hit_rate']*100:.1f}%")
    print(f"Steps: {stats['steps']}")

    # Show audit log
    print("\n" + "=" * 70)
    print("AUDIT LOG (last 10 events)")
    print("=" * 70)
    audit = protection.get_audit_log()
    for event in audit[-10:]:
        event_type = event["event_type"]
        step = event.get("step", "?")
        data = event.get("data", {})
        name = data.get("name", "")
        print(f"[{event_type:15}] step {step:2} | {name}")


if __name__ == "__main__":
    asyncio.run(main())
