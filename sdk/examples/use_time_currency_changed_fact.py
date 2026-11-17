"""AF-012 demo: decide-time fact becomes invalid before a consequential call.

Shows a host-owned fact snapshot authorizing one tool call while the underlying
fake record is unchanged, then refusing a second distinct action after the
record mutates. Separate request identities keep ledger deduplication from
hiding the use-time check.

This validates declared fact dependencies at the side-effect boundary. It does
not prove the semantic truth of every model belief.

Run from the sdk directory::

    python examples/use_time_currency_changed_fact.py
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from mycelium.action_ledger import InMemoryLedgerStorage, ledger_sync, side_effect
from mycelium.transition import (
    SideEffectClass,
    ToolTransitionBinding,
    TransitionScope,
    execution_scope,
)
from mycelium.use_time_currency import (
    UseTimeCurrencyError,
    UseTimeCurrencyPolicy,
    UseTimeFactSpec,
    UseTimeToolPolicy,
    ValidatorResult,
    apply_use_time_currency,
    clear_captured_use_time_facts,
    register_use_time_validator,
    reset_use_time_currency_state,
    set_use_time_clock,
    use_time_facts,
)


def _policy() -> UseTimeCurrencyPolicy:
    return UseTimeCurrencyPolicy(
        enabled=True,
        missing_policy="error",
        policy_version="demo",
        tools={
            "refund_payment": UseTimeToolPolicy(
                facts=(
                    UseTimeFactSpec(
                        name="payment.refundable",
                        subject_type="payment",
                        id_from="payment_id",
                        validator="payment_state",
                        require={"value": True},
                        revision_from="payment_version",
                        max_age_seconds=30,
                    ),
                )
            )
        },
    )


def _binding() -> ToolTransitionBinding:
    return ToolTransitionBinding.for_tool(
        agent_id="demo",
        policy_version="demo",
        side_effect_class=SideEffectClass.KEYED_MUTATE,
    )


def main() -> None:
    reset_use_time_currency_state()
    clock = {"now": 1_000.0}
    set_use_time_clock(lambda: clock["now"])

    # Host-owned mutable record. The validator reads this at use time.
    record: dict[str, Any] = {"refundable": True, "version": "1"}
    bodies = {"n": 0}
    storage = InMemoryLedgerStorage()

    def payment_state(*, subject_id: str, **_kwargs: Any) -> ValidatorResult:
        del subject_id  # single local record for this walkthrough
        return ValidatorResult(
            current=bool(record["refundable"]),
            value=record["refundable"],
            revision=str(record["version"]),
        )

    register_use_time_validator("payment_state", payment_state)

    def refund_payment(payment_id: str, payment_version: str) -> dict[str, str]:
        bodies["n"] += 1
        with side_effect():
            return {
                "refunded": "true",
                "payment_id": payment_id,
                "payment_version": payment_version,
            }

    wrapped = apply_use_time_currency(
        ledger_sync(storage=storage, transition_binding=_binding())(refund_payment),
        _policy(),
        tool_name="refund_payment",
    )

    print("=== AF-012 use-time currency (changed decide-time fact) ===\n")
    print(
        "Validates declared dependencies at the side-effect boundary; "
        "not the semantic truth of every model belief.\n"
    )

    permitted = 0
    refused_changed = 0

    with execution_scope(TransitionScope(thread_id="demo", run_id="utc-demo", node="tools")):
        print("1) Capture host fact while the fake payment is refundable")
        use_time_facts.capture(
            name="payment.refundable",
            subject_type="payment",
            subject_id="pay_1",
            value=True,
            revision="1",
            max_age_seconds=30,
            require_value=True,
            observed_at=datetime.fromtimestamp(clock["now"], tz=timezone.utc),
        )
        print("   captured payment.refundable for pay_1")

        print("\n2) First consequential call while the record is unchanged")
        out = wrapped(
            payment_id="pay_1",
            payment_version="1",
            request_id="utc-demo-ok",
        )
        permitted = 1
        print(f"   permitted: {out}; bodies={bodies['n']}")

        print("\n3) Mutate the fake record before a second attempt")
        record["refundable"] = False
        print(f"   record now: {record}")

        # Decide-time snapshot still claims refundable=True; use-time recheck fails.
        use_time_facts.capture(
            name="payment.refundable",
            subject_type="payment",
            subject_id="pay_1",
            value=True,
            revision="1",
            max_age_seconds=30,
            require_value=True,
            observed_at=datetime.fromtimestamp(clock["now"], tz=timezone.utc),
        )
        print("\n4) Second call with a distinct request_id (dedupe must not hide this)")
        try:
            wrapped(
                payment_id="pay_1",
                payment_version="1",
                request_id="utc-demo-changed",
            )
        except UseTimeCurrencyError as exc:
            refused_changed = 1
            print(
                f"   REFUSED reason={exc.reason}; body not executed; "
                f"bodies={bodies['n']}"
            )
            print(f"   llm_message={exc.llm_message}")

        assert bodies["n"] == 1, "changed fact must refuse before a new effect"
        assert refused_changed == 1

        print(
            "\nExisting deny reasons (same policy; no new rules):\n"
            "- missing: required subject/capture basis absent\n"
            "- stale: age >= max_age_seconds at use\n"
            "- unverifiable: validator missing or cannot prove the required value\n"
        )

        print("5) Stale: advance controlled clock past max_age_seconds")
        record["refundable"] = True
        record["version"] = "1"
        use_time_facts.capture(
            name="payment.refundable",
            subject_type="payment",
            subject_id="pay_1",
            value=True,
            revision="1",
            max_age_seconds=30,
            require_value=True,
            observed_at=datetime.fromtimestamp(clock["now"], tz=timezone.utc),
        )
        clock["now"] = 1_040.0
        try:
            wrapped(
                payment_id="pay_1",
                payment_version="1",
                request_id="utc-demo-stale",
            )
        except UseTimeCurrencyError as exc:
            print(f"   REFUSED reason={exc.reason}; bodies={bodies['n']}")
        clock["now"] = 1_000.0

        print("\n6) Missing: empty required subject id")
        clear_captured_use_time_facts()
        try:
            wrapped(
                payment_id="",
                payment_version="1",
                request_id="utc-demo-missing",
            )
        except UseTimeCurrencyError as exc:
            print(f"   REFUSED reason={exc.reason}; bodies={bodies['n']}")

        print("\n7) Unverifiable: drop the registered validator")
        reset_use_time_currency_state()
        set_use_time_clock(lambda: clock["now"])
        record["refundable"] = True
        record["version"] = "1"
        # Rebuild the wrapped tool under a fresh policy; do not re-register
        # payment_state so use-time validation cannot prove the fact.
        wrapped_no_validator = apply_use_time_currency(
            ledger_sync(storage=InMemoryLedgerStorage(), transition_binding=_binding())(
                refund_payment
            ),
            _policy(),
            tool_name="refund_payment",
        )
        use_time_facts.capture(
            name="payment.refundable",
            subject_type="payment",
            subject_id="pay_1",
            value=True,
            revision="1",
            max_age_seconds=30,
            require_value=True,
            observed_at=datetime.fromtimestamp(clock["now"], tz=timezone.utc),
        )
        try:
            wrapped_no_validator(
                payment_id="pay_1",
                payment_version="1",
                request_id="utc-demo-unverifiable",
            )
        except UseTimeCurrencyError as exc:
            print(f"   REFUSED reason={exc.reason}; bodies={bodies['n']}")

    print(
        f"\nwalkthrough ok: permitted={permitted} refused_changed={refused_changed} "
        f"bodies={bodies['n']}"
    )


if __name__ == "__main__":
    main()
