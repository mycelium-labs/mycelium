"""
AutoGen Integration Example

Shows how to use Mycelium AF-006 protection with AutoGen multi-agent systems.
"""

import asyncio
from mycelium.protections import tool
from mycelium.adapters.autogen import AutoGenIntegration


# Define tools
@tool(critical=True, invalidate_after_steps=5)
async def get_stock_price(symbol: str) -> dict:
    """Get current stock price (critical, always re-verify)."""
    print(f"    → Fetching stock price for {symbol}")
    return {
        "symbol": symbol,
        "price": 150.25,
        "timestamp": "2026-05-03T10:00:00Z",
    }


@tool(critical=False, invalidate_after_steps=10)
async def analyze_sentiment(text: str) -> dict:
    """Analyze sentiment of text (non-critical)."""
    print(f"    → Analyzing sentiment of {len(text)} chars")
    return {
        "sentiment": "positive",
        "score": 0.85,
        "keywords": ["excellent", "promising"],
    }


@tool(critical=False, invalidate_after_steps=15)
async def fetch_news(symbol: str) -> list:
    """Fetch recent news about stock (non-critical, longer TTL)."""
    print(f"    → Fetching news for {symbol}")
    return [
        {"title": "Strong earnings beat", "date": "2026-05-01"},
        {"title": "Analyst upgrade", "date": "2026-04-30"},
    ]


async def agent_1_task(protection, symbol: str) -> str:
    """Agent 1: Get price and analyze sentiment."""
    print(f"\n  [AGENT 1] Analyzing {symbol}")

    # Get stock price (critical)
    price = await protection.call_tool_protected(
        "get_stock_price", get_stock_price, symbol=symbol
    )
    print(f"    Price: ${price['price']}")

    # Get news and analyze sentiment
    news = await protection.call_tool_protected(
        "fetch_news", fetch_news, symbol=symbol
    )
    print(f"    Found {len(news)} news items")

    if news:
        sentiment = await protection.call_tool_protected(
            "analyze_sentiment", analyze_sentiment, text=news[0]["title"]
        )
        print(f"    Sentiment: {sentiment['sentiment']} (score: {sentiment['score']})")

    return f"Agent 1 analysis complete for {symbol}"


async def agent_2_task(protection, symbol: str) -> str:
    """Agent 2: Verify price (should hit cache) and get fresh news."""
    print(f"\n  [AGENT 2] Verification for {symbol}")

    # Verify price (should hit cache)
    print("    Re-verifying price (cached)...")
    price = await protection.call_tool_protected(
        "get_stock_price", get_stock_price, symbol=symbol
    )
    print(f"    ✓ Price confirmed: ${price['price']}")

    # Get news again (cached from agent 1)
    print("    Retrieving news (cached)...")
    news = await protection.call_tool_protected(
        "fetch_news", fetch_news, symbol=symbol
    )
    print(f"    ✓ Found {len(news)} news items")

    return f"Agent 2 verification complete for {symbol}"


async def agent_3_task(protection, symbol: str) -> str:
    """Agent 3: Fresh price check after TTL expiration."""
    print(f"\n  [AGENT 3] Fresh analysis for {symbol}")

    # Price should still be cached (TTL=5 steps, only 4 steps passed)
    price = await protection.call_tool_protected(
        "get_stock_price", get_stock_price, symbol=symbol
    )
    print(f"    Price: ${price['price']}")

    return f"Agent 3 fresh analysis for {symbol}"


async def main():
    """Simulate AutoGen multi-agent system with protection."""
    print("=" * 70)
    print("AutoGen Integration Example")
    print("=" * 70)

    # Initialize integration
    integration = AutoGenIntegration(verbose=True)
    integration.register_tools(
        {
            "get_stock_price": get_stock_price,
            "analyze_sentiment": analyze_sentiment,
            "fetch_news": fetch_news,
        },
        critical_tools=["get_stock_price"],
    )

    protection = integration.get_protection()

    print("\n[AUTOGEN] Starting multi-agent conversation\n")

    symbol = "AAPL"

    # Agent 1: Initial analysis
    await agent_1_task(protection, symbol)
    protection.handle_message()

    # Agent 2: Verification (should hit cache)
    await agent_2_task(protection, symbol)
    protection.handle_message()

    # Idle messages (advance steps without tool calls)
    print("\n  [AUTOGEN] Idle message exchanges (3 steps)")
    for i in range(3):
        protection.handle_message()
        print(f"    Message {i+1} exchanged")

    # Agent 3: Fresh analysis (price may be expired after 5 steps)
    print(f"\n  [AUTOGEN] After 5 message steps (TTL threshold)")
    await agent_3_task(protection, symbol)
    protection.handle_message()

    # Show stats
    stats = integration.get_protection().get_stats()
    print("\n" + "=" * 70)
    print("AUTOGEN STATS")
    print("=" * 70)
    print(f"Cache entries: {stats['cache_entries']}")
    print(f"Cache hits: {stats['cache_hits']}")
    print(f"Cache misses: {stats['cache_misses']}")
    print(f"Hit rate: {stats['hit_rate']*100:.1f}%")
    print(f"Messages processed: {stats['messages_processed']}")

    # Show audit log
    print("\n" + "=" * 70)
    print("AUDIT LOG (last 15 events)")
    print("=" * 70)
    audit = protection.get_audit_log()
    for event in audit[-15:]:
        event_type = event["event_type"]
        step = event.get("step", "?")
        data = event.get("data", {})
        name = data.get("name", "")
        if name:
            print(f"[{event_type:15}] step {step:2} | {name}")


if __name__ == "__main__":
    asyncio.run(main())
