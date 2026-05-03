"""
Smolagents Integration Example

Shows how to use Mycelium AF-006 protection with Smolagents framework.
"""

import asyncio
from mycelium.protections import tool
from mycelium.adapters.smolagents import SmolagentsIntegration


# Define tools
@tool(critical=True, invalidate_after_steps=5)
async def search_documents(query: str) -> list:
    """Search knowledge base documents (critical)."""
    print(f"    → Searching documents for: {query}")
    return [
        {"id": "doc_001", "title": "Getting Started Guide", "relevance": 0.98},
        {"id": "doc_002", "title": "API Reference", "relevance": 0.87},
    ]


@tool(critical=False, invalidate_after_steps=10)
async def summarize_document(doc_id: str) -> dict:
    """Get document summary (non-critical)."""
    print(f"    → Summarizing document {doc_id}")
    return {
        "doc_id": doc_id,
        "summary": "This document provides comprehensive information about the system.",
        "word_count": 2500,
        "reading_time_minutes": 10,
    }


@tool(critical=False, invalidate_after_steps=15)
async def get_document_metadata(doc_id: str) -> dict:
    """Get document metadata (non-critical, longer TTL)."""
    print(f"    → Fetching metadata for {doc_id}")
    return {
        "doc_id": doc_id,
        "author": "Documentation Team",
        "last_updated": "2026-05-01",
        "version": "2.1.0",
    }


async def search_action(protection, query: str) -> str:
    """Agent action: search and summarize."""
    print(f"\n  [ACTION] Searching: {query}")

    # Search documents (critical)
    docs = await protection.call_tool_protected(
        "search_documents", search_documents, query=query
    )
    print(f"    Found {len(docs)} documents")

    # Get metadata for first result
    if docs:
        meta = await protection.call_tool_protected(
            "get_document_metadata",
            get_document_metadata,
            doc_id=docs[0]["id"],
        )
        print(f"    Document version: {meta['version']}")

    return f"Search complete for: {query}"


async def summarize_action(protection, doc_id: str) -> str:
    """Agent action: summarize document."""
    print(f"\n  [ACTION] Summarizing {doc_id}")

    # Get summary
    summary = await protection.call_tool_protected(
        "summarize_document", summarize_document, doc_id=doc_id
    )
    print(f"    Summary length: {summary['word_count']} words")

    return f"Summarized {doc_id}"


async def refined_search_action(protection, query: str) -> str:
    """Agent action: refined search (should hit cache)."""
    print(f"\n  [ACTION] Refined search: {query}")

    # Re-search (should hit cache)
    print("    Re-searching (cached)...")
    docs = await protection.call_tool_protected(
        "search_documents", search_documents, query=query
    )
    print(f"    ✓ Found {len(docs)} documents (cached)")

    return f"Refined search complete for: {query}"


async def main():
    """Simulate Smolagents agent with protection."""
    print("=" * 70)
    print("Smolagents Integration Example")
    print("=" * 70)

    # Initialize integration
    integration = SmolagentsIntegration(verbose=True)
    integration.register_tools(
        {
            "search_documents": search_documents,
            "summarize_document": summarize_document,
            "get_document_metadata": get_document_metadata,
        },
        critical_tools=["search_documents"],
    )

    protection = integration.get_protection()

    print("\n[SMOLAGENTS] Starting agent reasoning loop\n")

    query = "Python best practices"

    # Action 1: Search
    await search_action(protection, query)
    protection.advance_action()

    # Action 2: Summarize
    await summarize_action(protection, "doc_001")
    protection.advance_action()

    # Action 3: Refine search (cached from action 1)
    await refined_search_action(protection, query)
    protection.advance_action()

    # Idle reasoning steps
    print("\n  [SMOLAGENTS] Internal reasoning (2 steps)")
    for i in range(2):
        protection.advance_action()
        print(f"    Reasoning step {i+1}")

    # Action 4: After TTL expiration
    print(f"\n  [SMOLAGENTS] After 5 actions (search TTL threshold)")
    await search_action(protection, f"{query} - updated")
    protection.advance_action()

    # Show stats
    stats = integration.get_stats()
    print("\n" + "=" * 70)
    print("SMOLAGENTS STATS")
    print("=" * 70)
    print(f"Cache entries: {stats['cache_entries']}")
    print(f"Cache hits: {stats['cache_hits']}")
    print(f"Cache misses: {stats['cache_misses']}")
    print(f"Hit rate: {stats['hit_rate']*100:.1f}%")
    print(f"Actions: {stats['actions']}")

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
