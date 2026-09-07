# LangGraph email onboarding (local simulation)

This deterministic walkthrough is: inspect coverage → preview → execute through
the configured wrapper → inspect the outcome. It never sends email or requires
a model API. The interruption section is a scripted simulation, not proof for
every crash schedule; an actual `UNKNOWN` remains blocked until reconciliation
or operator resolution.

From `sdk/`:

```bash
python -m mycelium coverage -c examples/langgraph_email_onboarding/mycelium.yaml
printf '{"to":"alice@example.test","subject":"Hello","body":"Welcome"}\n' >/tmp/email-ok.json
python -m mycelium preview --tool send_email --args-file /tmp/email-ok.json -c examples/langgraph_email_onboarding/mycelium.yaml
printf '{"to":"mallory@blocked.test","subject":"Hello","body":"Welcome"}\n' >/tmp/email-denied.json
python -m mycelium preview --tool send_email --args-file /tmp/email-denied.json -c examples/langgraph_email_onboarding/mycelium.yaml --json
python examples/langgraph_email_onboarding/run.py
```

Preview is a snapshot: it does not consume grants, budgets, or a ledger row,
and execution re-evaluates policies at the atomic claim boundary. To connect a
real tool, provide host-owned stable action identity, capability, authorized
destinations, policy, and provider idempotency or read-only reconciliation
support. Calls bypassing the wrapper are outside Mycelium coverage.
