"""
CrewAI integration — use protect_sync on tools Crew calls via BaseTool._run.

No adapter: decorate the underlying function; Crew invokes it with normal kwargs.
"""

from __future__ import annotations

import sys
from pathlib import Path

SDK = Path(__file__).resolve().parent.parent / "sdk"
if str(SDK) not in sys.path:
    sys.path.insert(0, str(SDK))

from mycelium import Session, protect_sync
from mycelium.protect import _session_var

_COMPANIES: dict[str, dict] = {
    "Acme": {"company": "Acme", "industry": "Technology"},
}


@protect_sync(entity_param="company_name", ttl=120)
def get_company_info(company_name: str) -> dict:
    return dict(_COMPANIES[company_name])


def research_task(company_name: str) -> str:
    """Crew-shaped task body — bind a Session for sync tools (see sdk/README)."""
    session = Session()
    token = _session_var.set(session)
    try:
        info = get_company_info(company_name=company_name)
        info2 = get_company_info(company_name=company_name)  # cache hit
    finally:
        _session_var.reset(token)
    assert info == info2
    return f"{info['company']}: {info['industry']}"


def main() -> None:
    print("CrewAI pattern: BaseTool._run → your @protect_sync function\n")
    print(research_task("Acme"))


if __name__ == "__main__":
    main()
