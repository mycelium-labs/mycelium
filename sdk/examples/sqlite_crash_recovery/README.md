# Plain-Python SQLite crash + operator recovery

Adoption walkthrough — not a new capability. A durable-local rehearsal of the
same process-kill / crash-window pattern covered by
`tests/test_process_kill_crash_window.py`, without LangGraph or Redis.

| Piece | What you get |
|-------|----------------|
| Host-owned `request_id` | Stable action id reused across process invocations |
| SQLite ledger | Durable claims on a single node (`SqliteLedgerStorage`) |
| Independent fake provider | Separate SQLite file that records effects (no dedupe, no network) |
| Worker-death policy | `reclaim_requires_death_signal: true` — lease age alone is never proof of safety |
| Operator release | Inspect the fake provider, record `verified=completed`, redispatch returns it |

Contrast:

- [`../failure_cases/`](../failure_cases/) — in-process memory storage
- [`../langgraph_redis_crash/`](../langgraph_redis_crash/) — LangGraph + Redis + receipts

## One-shot demo

From `sdk/` (or any env with the package on `PYTHONPATH`):

```bash
python examples/sqlite_crash_recovery/run.py
```

Expected markers (exit code 0):

```text
[ok] crash - killed worker after provider effect; provider_effects=1 ...
[ok] recover-refuse - release_without_death refused: LedgerWorkerAliveError ...
[ok] mark-dead - override_heartbeat=True ...
[ok] redispatch - outcome=HARD_BLOCK provider_effects=1 body_ran=False ...
[ok] recover - release verified=completed ...
[ok] redispatch - outcome=RETURN provider_effects=1 body_ran=False ...
[ok] walkthrough complete - provider_effects=1 ambiguous_hard_block=True verified_return=True
```

Cleanup is limited to the printed temporary workspace:

```bash
rm -rf /tmp/mycelium-sqlite-crash-...
```

No credentials, network, Redis, Docker, or external services.

## Step-by-step (separate invocations)

Use one temporary workspace and keep the same host-owned action id:

```bash
WORK=$(mktemp -d /tmp/mycelium-sqlite-crash-XXXX)
export MYCELIUM_SQLITE_CRASH_WORKSPACE="$WORK"
cd sdk   # repository checkout

python examples/sqlite_crash_recovery/run.py init
# optional: pin the id — python ... init --action-id charge-order:ORD-demo-001
ACTION_ID=$(cat "$WORK/action_id.txt")
echo "action_id=$ACTION_ID"
```

### 1. Worker: effect recorded, then hang (crash window)

```bash
python examples/sqlite_crash_recovery/run.py worker &
WORKER_PID=$!
# Wait until the fake provider has recorded the effect and the ready marker exists:
while [ ! -f "$WORK/worker.ready" ]; do sleep 0.05; done
# Simulated crash after provider effect, before ledger completion:
kill -9 $WORKER_PID
wait $WORKER_PID 2>/dev/null || true
# Short demo lease (1s) must lapse so redispatch sees EXPIRED, not POLL:
sleep 1.5
```

Expected counts after the kill:

| Check | Expected |
|-------|----------|
| Fake provider effects | `1` |
| Ledger completion | none (entry still in-flight / expired, `maybe_crossed`) |

```bash
python examples/sqlite_crash_recovery/run.py status
# provider_effects=1, terminal typically EXPIRED, boundary=maybe_crossed
```

### 2. Lease age alone is not enough

```bash
python examples/sqlite_crash_recovery/run.py recover-refuse
```

Expected:

```text
[ok] recover-refuse - release_without_death refused: LedgerWorkerAliveError (lease age alone is not proof of safety)
```

### 3. Assert worker death (not lease age)

```bash
python examples/sqlite_crash_recovery/run.py mark-dead
```

Expected:

```text
[ok] mark-dead - override_heartbeat=True ... terminal=EXPIRED
```

Death assertion records that the operator killed the worker. It does **not**
authorize another provider execution by itself.

### 4. Ambiguous redispatch refuses another effect

```bash
python examples/sqlite_crash_recovery/run.py redispatch
```

Expected:

```text
[ok] redispatch - outcome=HARD_BLOCK provider_effects=1 body_ran=False ...
```

### 5. Inspect fake provider and record verified completed

```bash
python examples/sqlite_crash_recovery/run.py recover
```

Expected:

```text
[ok] recover - provider_inspect effects=1 receipt={...}
[ok] recover - release verified=completed ...
```

Equivalent CLI against the same SQLite file (optional; still no network):

```bash
mycelium transitions show "$ACTION_ID" --sqlite "$WORK/ledger.sqlite"
mycelium transitions mark-dead "$ACTION_ID" \
  --sqlite "$WORK/ledger.sqlite" \
  --by ops@example.test \
  --reason "operator killed the worker after the ready marker" \
  --override-heartbeat
mycelium transitions release "$ACTION_ID" \
  --sqlite "$WORK/ledger.sqlite" \
  --verified completed \
  --result-json "$(python -c "import sqlite3,os; p=os.environ['MYCELIUM_SQLITE_CRASH_WORKSPACE']+'/provider.sqlite'; c=sqlite3.connect(p); print(c.execute('select receipt from effects').fetchone()[0])")" \
  --by ops@example.test \
  --reason "verified local fake provider receipt"
```

Use `mark-dead` while the row is still `EXPIRED`. After a `HARD_BLOCK` redispatch
the row may already be `BLOCKED`; then release alone finishes recovery. Never
treat lease expiry by itself as authorization to run the tool again.

### 6. Settled redispatch returns the verified result

```bash
python examples/sqlite_crash_recovery/run.py redispatch
```

Expected:

```text
[ok] redispatch - outcome=RETURN provider_effects=1 body_ran=False ...
```

Provider effect count stays **1**. The tool body does not run again.

### Cleanup

```bash
rm -rf "$WORK"
unset MYCELIUM_SQLITE_CRASH_WORKSPACE
```

## Layout

| Path | Role |
|------|------|
| `ledger.sqlite` | Mycelium transition ledger |
| `provider.sqlite` | Independent fake provider (effects only) |
| `action_id.txt` | Stable host-owned action id |
| `mycelium.yaml` | Optional CLI config pointing at this workspace |
| `worker.ready` | Crash-window ready marker |

## Related

- Process-kill tests: `sdk/tests/test_process_kill_crash_window.py`
- Operator runbook: [sdk/README.md § Operator runbook](../../README.md#operator-runbook-your-agent-hard-blocked)
- Gate table: [sdk/README.md § Resolution gates](../../README.md#resolution-gates)
