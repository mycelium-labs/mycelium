"""Authority-window expiry: authorization is not still valid at use.

An integrator can obtain a host authorization early in a run and then stall on
a model call, a queue, or a human review. When the tool is finally invoked the
grant may already have expired. Mycelium re-validates time-bounded authority at
the side-effect boundary, so the stale call is refused before any provider
effect instead of silently proceeding.

This walkthrough uses the shipped authority-window APIs and a controlled clock.
It does not extend, renew, or substitute authority.

Run from the sdk directory::

    python examples/authority_window_expiry.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mycelium.authority_window import (
    AuthorityExpiredError,
    AuthorityWindowPolicy,
    BoundAuthority,
    register_authority_for_use,
    reset_authority_window_state,
    set_authority_clock,
    set_authority_window_policy,
    validate_authority_at_use,
)


def main() -> None:
    reset_authority_window_state()

    # Only host/test code owns time. Never use a model-supplied clock here.
    clock = {"now": datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)}
    set_authority_clock(lambda: clock["now"].timestamp())
    set_authority_window_policy(
        AuthorityWindowPolicy(enabled=True, use_time_check="required")
    )

    provider_calls = {"n": 0}

    def send_refund(amount_cents: int) -> dict[str, object]:
        """Stand-in for a consequential provider call."""
        provider_calls["n"] += 1
        return {"refunded_cents": amount_cents}

    grant = BoundAuthority(
        authority_id="grant-demo-1",
        authority_kind="destructive_grant",
        expires_at=clock["now"] + timedelta(minutes=10),
        tool="send_refund",
        operation="refund",
        object_ref="payment:pay_1",
        policy_version="demo",
    )
    register_authority_for_use(grant)

    print("=== authority window: expiry before use ===\n")
    print(
        "Authority is checked at authorize and again immediately before the\n"
        "side effect. Expiry narrows validity; it is never extended.\n"
    )
    print(f"grant expires_at: {grant.expires_at.isoformat()}")

    print("\n1) Call inside the authority window")
    decision = validate_authority_at_use()
    print(f"   decision={decision.decision} reason={decision.reason}")
    print(f"   provider: {send_refund(amount_cents=2500)}; calls={provider_calls['n']}")

    print("\n2) Operator work takes longer than the grant allowed")
    clock["now"] = clock["now"] + timedelta(minutes=11)
    print(f"   host clock is now {clock['now'].isoformat()}")

    print("\n3) The same stale grant is refused before the provider call")
    try:
        validate_authority_at_use()
    except AuthorityExpiredError as exc:
        print(f"   REFUSED phase={exc.phase}; expires_at={exc.expires_at.isoformat()}")
        print(f"   llm_message={exc.llm_message}")
        print(f"   recovery_hint={exc.recovery_hint}")
        print(f"   provider calls still {provider_calls['n']}")

    assert provider_calls["n"] == 1, "expired authority must not reach the provider"

    print(
        "\nThe host must issue a new grant with a future expires_at. Mycelium will\n"
        "not renew or extend the expired one for you."
    )
    print(f"\nwalkthrough ok: provider_calls={provider_calls['n']} expired_refusals=1")

    reset_authority_window_state()


if __name__ == "__main__":
    main()
