"""
LangGraph integration — no Mycelium adapter.

Register @protect-decorated callables in ToolNode (or call them inside nodes).
Mycelium intercepts at the function boundary; the graph code stays unchanged.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SDK = Path(__file__).resolve().parent.parent / "sdk"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from mycelium import protect, Session

_DB = {"alice": {"user_id": "alice", "theme": "dark"}}


@protect(entity_param="user_id", ttl=60)
async def fetch_user_profile(user_id: str) -> dict:
    return dict(_DB[user_id])


@protect(critical=True)
async def check_rate_limit() -> dict:
    return {"requests_remaining": 1000}


async def fetch_context_node(user_id: str, query: str) -> dict:
    """Example graph node — call protected tools inside Session."""
    async with Session():
        profile = await fetch_user_profile(user_id=user_id)
        limit = await check_rate_limit()
    return {"profile": profile, "limit": limit, "query": query}


async def main() -> None:
    print("LangGraph pattern: ToolNode([fetch_user_profile, check_rate_limit])")
    print("Each node: async with Session(): await your_tool(...)\n")

    state = await fetch_context_node("alice", "summarize preferences")
    print("context:", state)


if __name__ == "__main__":
    asyncio.run(main())
