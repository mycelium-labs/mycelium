"""
Minimal AF-006 integration: Session + @protect + MessageValidator before LLM.

See docs/af006-integration.md for the full recipe.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

SDK = Path(__file__).resolve().parent.parent / "sdk"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from mycelium import MessageValidator, protect, Session

_DB: dict[str, dict] = {
    "c1": {"customer_id": "c1", "plan": "pro"},
}


@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return dict(_DB[customer_id])


async def main() -> None:
    validator = MessageValidator()
    messages: list[dict] = [
        {"role": "user", "content": "What plan is customer c1 on?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "fetch_customer", "arguments": '{"customer_id": "c1"}'},
                }
            ],
        },
    ]

    async with Session() as session:
        row = await fetch_customer(customer_id="c1")
        await fetch_customer(customer_id="c1")  # cache hit
        messages.append(
            {"role": "tool", "tool_call_id": "call_1", "content": str(row)},
        )
        events = [e["event"] for e in session.audit_log()]
        assert "cache_hit" in events

    messages = validator.repair(messages)
    print("OK — tool cache + message repair; ready for LLM call")
    print(f"  messages: {len(messages)} turns")
    print(f"  audit sample: {events[:3]}...")


if __name__ == "__main__":
    asyncio.run(main())
