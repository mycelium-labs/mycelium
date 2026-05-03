"""
OpenAI Agents SDK Integration Example

Shows how to use Mycelium AF-006 protection with OpenAI Agents SDK.
"""

import asyncio
from mycelium.protections import tool
from mycelium.adapters.openai_agents import OpenAIAgentsIntegration


# Define tools
@tool(critical=True, invalidate_after_steps=5)
async def get_user_balance(user_id: str) -> dict:
    """Get user account balance (critical)."""
    print(f"    → Fetching balance for user {user_id}")
    return {
        "user_id": user_id,
        "balance": 1500.50,
        "currency": "USD",
        "last_updated": "2026-05-03T10:00:00Z",
    }


@tool(critical=False, invalidate_after_steps=10)
async def get_transaction_history(user_id: str, limit: int = 5) -> list:
    """Get transaction history (non-critical)."""
    print(f"    → Fetching {limit} transactions for user {user_id}")
    return [
        {"id": "txn_1", "amount": -50.00, "date": "2026-05-03"},
        {"id": "txn_2", "amount": 100.00, "date": "2026-05-02"},
        {"id": "txn_3", "amount": -25.00, "date": "2026-05-01"},
    ]


@tool(critical=False, invalidate_after_steps=15)
async def get_fraud_score(user_id: str) -> dict:
    """Get fraud risk assessment (non-critical, longer TTL)."""
    print(f"    → Calculating fraud score for user {user_id}")
    return {
        "user_id": user_id,
        "fraud_score": 0.15,
        "risk_level": "low",
        "factors": ["multiple_attempts", "geographic_anomaly"],
    }


async def query_handler(protection, user_id: str, action: str) -> str:
    """Handle a user query with tool calls."""
    print(f"\n  [HANDLER] Processing: {action}")

    # Get balance (critical)
    balance = await protection.call_tool_protected(
        "get_user_balance", get_user_balance, user_id=user_id
    )
    print(f"    Balance: ${balance['balance']:.2f}")

    # Get transactions
    txns = await protection.call_tool_protected(
        "get_transaction_history",
        get_transaction_history,
        user_id=user_id,
        limit=5,
    )
    print(f"    Recent transactions: {len(txns)}")

    # Get fraud score
    fraud = await protection.call_tool_protected(
        "get_fraud_score", get_fraud_score, user_id=user_id
    )
    print(f"    Fraud risk: {fraud['risk_level']}")

    return f"Query processed for {user_id}"


async def cached_query_handler(protection, user_id: str) -> str:
    """Handle a follow-up query that should hit cache."""
    print(f"\n  [HANDLER] Cached query for {user_id}")

    # Re-check balance (should hit cache)
    print("    Re-verifying balance (cached)...")
    balance = await protection.call_tool_protected(
        "get_user_balance", get_user_balance, user_id=user_id
    )
    print(f"    ✓ Balance confirmed: ${balance['balance']:.2f}")

    return "Cached query complete"


async def main():
    """Simulate OpenAI Agents SDK with protection."""
    print("=" * 70)
    print("OpenAI Agents SDK Integration Example")
    print("=" * 70)

    # Initialize integration
    integration = OpenAIAgentsIntegration(verbose=True)
    integration.register_tools(
        {
            "get_user_balance": get_user_balance,
            "get_transaction_history": get_transaction_history,
            "get_fraud_score": get_fraud_score,
        },
        critical_tools=["get_user_balance"],
    )

    protection = integration.get_protection()

    print("\n[OPENAI AGENTS] Starting agent execution\n")

    user_id = "user_12345"

    # First query
    await query_handler(protection, user_id, "Check account status")
    protection.advance_step()

    # Cached follow-up
    await cached_query_handler(protection, user_id)
    protection.advance_step()

    # Idle steps
    print("\n  [OPENAI AGENTS] Processing other queries (3 steps)")
    for i in range(3):
        protection.advance_step()
        print(f"    Query {i+1} processed")

    # Query after TTL expiration
    print(f"\n  [OPENAI AGENTS] After 5 steps (balance TTL threshold)")
    await query_handler(protection, user_id, "Re-check account")
    protection.advance_step()

    # Show stats
    stats = integration.get_stats()
    print("\n" + "=" * 70)
    print("OPENAI AGENTS STATS")
    print("=" * 70)
    print(f"Cache entries: {stats['cache_entries']}")
    print(f"Cache hits: {stats['cache_hits']}")
    print(f"Cache misses: {stats['cache_misses']}")
    print(f"Hit rate: {stats['hit_rate']*100:.1f}%")
    print(f"Steps: {stats['steps']}")

    # Show audit log
    print("\n" + "=" * 70)
    print("AUDIT LOG (last 12 events)")
    print("=" * 70)
    audit = protection.get_audit_log()
    for event in audit[-12:]:
        event_type = event["event_type"]
        step = event.get("step", "?")
        data = event.get("data", {})
        name = data.get("name", "")
        if name:
            print(f"[{event_type:15}] step {step:2} | {name}")


if __name__ == "__main__":
    asyncio.run(main())
