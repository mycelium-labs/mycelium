# Resume a pinned item list

This local walkthrough publishes three JSON-compatible items with
`composite_items()`. SQLite stores the parent manifest, child results, and a
separate fake provider's effects. It needs no Redis, PostgreSQL, network access,
or credentials.

From the repository root, install the development SDK, then run the automatic
walkthrough from `sdk/`:

```bash
python -m pip install -e './sdk[dev]'
cd sdk
python examples/composite_items_resume/run.py
```

The automatic walkthrough uses a temporary directory. It interrupts after A
and B, rejects changed values and order, resumes with C, and replays the
completed batch. It reopens storage for each phase. The final provider effect
list stays `['A', 'B', 'C']` and the replay adds none:

```text
walkthrough ok: effects=['A', 'B', 'C'] replay_effects=0
```

## Separate invocations with durable local state

For the following commands, use a fresh state directory for the first run.
Keep the same directory and `run_id` for every subsequent invocation:

```bash
python examples/composite_items_resume/run.py --mode interrupt --state-dir /tmp/mycelium-items-demo --run-id items-demo
```

Expected output:

```text
mode: interrupt; run_id: items-demo
published A
published B
interrupted: stopped before C; A and B are durably completed
new provider effects: 2
provider effects: ['A', 'B']
```

Change either the first item's value or the list order while reusing the same
invocation identity:

```bash
python examples/composite_items_resume/run.py --mode change-value --state-dir /tmp/mycelium-items-demo --run-id items-demo
python examples/composite_items_resume/run.py --mode change-order --state-dir /tmp/mycelium-items-demo --run-id items-demo
```

Each command reports:

```text
rejected: item values/order differ from the pinned manifest
new provider effects: 0
provider effects: ['A', 'B']
```

Resume with the original list:

```bash
python examples/composite_items_resume/run.py --mode resume --state-dir /tmp/mycelium-items-demo --run-id items-demo
```

Expected output:

```text
mode: resume; run_id: items-demo
published C
completed: {"id": "C", "receipt": 3, "value": "gamma"}
new provider effects: 1
provider effects: ['A', 'B', 'C']
```

Run the same resume command again. It returns the stored result, reports zero
new provider effects, and prints no `published` line. A new `run_id` represents
a new batch and permits new effects.

## What this interruption shows

The deliberate interruption happens in a registered non-effect helper before
C is attempted. A and B already have durable completed child results, so their
tool bodies do not run again. The fake provider does not deduplicate requests:
its row count makes repeated executions visible.

This demonstrates controlled interruption between children. An actual crash
after a provider effect but before its result is stored requires reconciliation
or a hard block. See [durable composite recovery](../../docs/COMPOSITE_RECOVERY.md)
for those boundaries and the independent-process proof.
