"""Existing application callables; this module does not import Mycelium."""

import json
import os
from pathlib import Path
from urllib.request import urlopen


def tool(func):
    """Stand in for a framework's tool registration decorator."""
    return func


@tool
def send_notice(recipient: str, *, request_id: str | None = None) -> dict[str, str]:
    path = Path(os.environ["DISCOVERY_SPIKE_CALLS"])
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"recipient": recipient}) + "\n")
    return {"recipient": recipient, "status": "sent"}


@tool
def lookup_status(recipient: str) -> str:
    return f"unknown:{recipient}"


class ProviderClient:
    @tool
    def charge(self, amount: int) -> int:
        return amount


def hidden_provider_call(url: str) -> int:
    """A direct outbound call outside the registered callable boundary."""
    with urlopen(url) as response:
        return response.status
