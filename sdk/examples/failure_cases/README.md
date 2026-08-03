# Failure-case pack (AF-002 gates)

Teachable, **in-process** repros for Mycelium resolution gates. No Redis or
Postgres required — memory ledger only.

| # | Case | Gate | Failure without Mycelium | With Mycelium |
|---|------|------|--------------------------|---------------|
| 01 | Completed redispatch | **RETURN** | Charge / side effect runs twice | Second call returns stored result |
| 02 | Peer while owner held | **POLL** | Second worker fires in parallel | Peer waits; lease stays HELD |
| 03 | Ambiguous mutate crash | **HARD_BLOCK** | Blind retry may double-spend | Redispatch raises; no second body run |
| 04 | Incomplete durable record | **REPAIR** | Redispatch forks / re-executes | Heal fields, then RETURN — body once |
| 05 | Ambiguous + provider proof | reconcile → **RETURN** | Stuck or blind retry | Reconciler proves COMPLETED; no re-exec |

Public vocabulary often collapses to `ALLOW` / `REPAIR` / `SOFT_BLOCK` /
`HARD_BLOCK`. Mycelium also exposes `RETURN` and `POLL` as first-class
“do not execute again” gates — this pack makes those three partner-facing
outcomes (plus REPAIR / reconcile) runnable.

## Run

From the `sdk/` directory (package installed editable or on `PYTHONPATH`):

```bash
python examples/failure_cases/run_all.py
# or one case:
python examples/failure_cases/01_return_completed.py
python examples/failure_cases/02_poll_in_flight.py
python examples/failure_cases/03_hard_block_unknown.py
python examples/failure_cases/04_repair_incomplete.py
python examples/failure_cases/05_reconcile_completed.py
```

Each script exits `0` on proof success and prints the gate name.

## Related

- Feature tour: `mycelium demo` (same proofs + operator release + unguarded baseline)
- Cloud-style two-worker Redis: `mycelium demo --redis`
- Gate table: [sdk/README.md § Resolution gates](../../README.md#resolution-gates)
- Threat model: [docs/FAILURE_AND_THREAT_MODEL.md](../../docs/FAILURE_AND_THREAT_MODEL.md)
