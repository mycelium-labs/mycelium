"""AF-008 demo: frozen run allowlist blocks mid-run tool widen.

Run from the sdk directory::

    python examples/scope_guard_allowlist.py
"""

from __future__ import annotations

from mycelium.scope_guard import (
    InMemoryScopeGuardStorage,
    ScopeGrant,
    ScopeGuard,
    scope_guard_sync,
)
from mycelium.tool_boundary import ToolBoundaryError
from mycelium.tool_registry import ToolRegistry
from mycelium.transition import TransitionScope, execution_scope


def main() -> None:
    registry = ToolRegistry(allowed=["fetch_customer"])
    guard = ScopeGuard(
        InMemoryScopeGuardStorage(),
        default_grant=ScopeGrant(allowed_tools=registry.allowed_tools),
    )

    @scope_guard_sync(guard)
    def fetch_customer(customer_id: str) -> str:
        return customer_id

    @scope_guard_sync(guard, tool_name="admin_wipe")
    def admin_wipe() -> str:
        return "wiped"

    print("=== AF-008 scope guard (run allowlist freeze) ===\n")
    with execution_scope(TransitionScope(thread_id="t", run_id="demo-1", node="tools")):
        print("1) fetch_customer within frozen grant -> ok")
        print(f"   {fetch_customer(customer_id='c1')!r}")

        print("\n2) Host widens ToolRegistry mid-run (handoff mistake)")
        registry.allow("admin_wipe")
        print(f"   registry now allows: {sorted(registry.allowed_tools)}")

        print("3) admin_wipe still blocked by frozen grant")
        try:
            admin_wipe()
        except ToolBoundaryError as exc:
            print(f"   BLOCKED violation={exc.violation}")


if __name__ == "__main__":
    main()
