"""
CrewAI Integration Example

Shows Mycelium AF-006 protection with CrewAI agents.
"""

import asyncio
from mycelium.protections import tool
from mycelium.adapters.crewai import CrewAIIntegration, CrewAITaskExecutor


# Define tools
@tool(critical=True, invalidate_after_steps=5)
async def get_company_info(company_name: str) -> dict:
    """Get company information."""
    print(f"    → Fetching company info for {company_name}")
    return {
        "company": company_name,
        "industry": "Technology",
        "employees": 5000,
    }


@tool(critical=False, invalidate_after_steps=10)
async def search_market_data(query: str) -> list:
    """Search market data."""
    print(f"    → Searching market data for: {query}")
    return [
        {"ticker": "AAPL", "price": 150.25},
        {"ticker": "GOOGL", "price": 140.30},
    ]


@tool(critical=False, invalidate_after_steps=3)
async def get_news(company: str) -> list:
    """Get recent news."""
    print(f"    → Fetching news for {company}")
    return [
        {"title": "Company announces Q4 results", "date": "2026-05-01"},
        {"title": "New product launch", "date": "2026-04-28"},
    ]


async def research_task(protection, company_name: str) -> str:
    """Research task: gather company and market data."""
    print(f"\n  [TASK] Research: {company_name}")

    # Gather company info
    info = await protection.call_tool_protected(
        "get_company_info", get_company_info, company_name=company_name
    )
    print(f"    Company: {info['company']}, Employees: {info['employees']}")

    # Get market data
    market = await protection.call_tool_protected(
        "search_market_data", search_market_data, query=company_name
    )
    print(f"    Found {len(market)} market entries")

    # Get news
    news = await protection.call_tool_protected(
        "get_news", get_news, company=company_name
    )
    print(f"    Found {len(news)} news items")

    return f"Research complete: {info['company']}"


async def analysis_task(protection, company_name: str) -> str:
    """Analysis task: reuse cached data."""
    print(f"\n  [TASK] Analysis: {company_name}")

    # Reuse company info (should hit cache)
    print("    Retrieving company info (cached)...")
    info = await protection.call_tool_protected(
        "get_company_info", get_company_info, company_name=company_name
    )
    print(f"    ✓ Company: {info['company']}")

    return f"Analysis complete for {company_name}"


async def main():
    """Simulate CrewAI crew execution with protection."""
    print("=" * 70)
    print("CrewAI Integration Example")
    print("=" * 70)

    # Initialize integration
    integration = CrewAIIntegration(verbose=True)
    integration.register_tools(
        {
            "get_company_info": get_company_info,
            "search_market_data": search_market_data,
            "get_news": get_news,
        },
        critical_tools=["get_company_info"],
    )

    protection = integration.get_protection()

    print("\n[CREW] Starting crew execution\n")

    # Simulate crew with multiple tasks
    company = "Apple Inc"

    # Task 1: Research
    await research_task(protection, company)
    protection.advance_task()

    # Task 2: Analysis (reuses cached data)
    await analysis_task(protection, company)
    protection.advance_task()

    # Task 3: More research after cache expires
    print(f"\n  [TASK] Research again (after cache age > TTL)")
    info = await protection.call_tool_protected(
        "get_company_info", get_company_info, company_name=company
    )
    print(f"    ✓ Company refetched: {info['company']}")
    protection.advance_task()

    # Show stats
    stats = integration.get_crew_stats()
    print("\n" + "=" * 70)
    print("CREW STATS")
    print("=" * 70)
    print(f"Tasks completed: {stats['tasks_completed']}")
    print(f"Cache entries: {stats['cache_entries']}")
    print(f"Cache hits: {stats['cache_hits']}")
    print(f"Cache misses: {stats['cache_misses']}")
    print(f"Hit rate: {stats['hit_rate']*100:.1f}%")

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
