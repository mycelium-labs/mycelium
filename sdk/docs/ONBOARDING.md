# Developer onboarding: inspect, preview, execute, explain

Mycelium protects a tool action across its lifecycle. Start with configured
metadata, preview an input without invoking the tool, execute through the
runtime wrapper, then inspect the durable transition or outcome.

```bash
mycelium coverage --config mycelium.yaml
mycelium preview --tool send_email --args-file email.json --config mycelium.yaml
mycelium run --config mycelium.yaml -- python -m your_agent
mycelium transitions list --config mycelium.yaml
```

`coverage` separates configured protections from runtime verification. A
missing association is `unknown`; static detection never proves every call uses
the wrapper. `preview` uses only the preflight loader and declarative contract
and destination validators. It never imports customer callables, invokes a
provider, creates a ledger row, consumes grants, reserves budgets, resolves
credentials, or runs reconciliation. Its outcome is always `not_executed`.

`denied` means an evaluated required check failed. `indeterminate` means no
denial is known but execution-only checks remain. `permitted_at_preview` means
the applicable preview checks passed for the observed snapshot. Neither result
is authorization or a reservation; execution re-evaluates controls at the
shared atomic boundary. `UNKNOWN` means the effect may have happened and must
follow read-only reconciliation or operator resolution. It is not safe-to-retry
guidance.

See the complete deterministic
[`langgraph_email_onboarding`](../examples/langgraph_email_onboarding/README.md)
example. A real integration must supply host-owned stable action identity,
capability, authorized destinations, policy, and provider idempotency or
reconciliation support. Direct provider calls bypass Mycelium coverage.
