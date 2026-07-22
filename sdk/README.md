# Mycelium runtime

[![PyPI version](https://img.shields.io/pypi/v/mycelium-runtime.svg?cacheSeconds=60&release=1.13.3)](https://pypi.org/project/mycelium-runtime/)
[![Python](https://img.shields.io/pypi/pyversions/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)

Current package: **mycelium-runtime v1.13.3** (`REPAIR` gate + command auto-instrumentation + transition envelope).

## One painful bug → a few lines of config

**LangGraph Cloud redispatches a long tool call while the first is still running.** Both complete. You pay twice. Side effects run twice. [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417)

Mycelium’s answer is a **transition envelope**, not “idempotency key + cached result” alone: classify the tool (**side-effect class**), hold an execution **lease** while work is in flight, record **terminal state** (`IN_FLIGHT` / `COMPLETED` / `UNKNOWN` / …), and **hard-block** (or reconcile) when a mutating redispatch would be unsafe. Same key while in-flight → poll; completed → return stored; ambiguous payment-class → stop.

On LangGraph Cloud, long tool calls can be redispatched on the order of **~180s**, aligned with the platform’s **`BG_JOB_HEARTBEAT`** sweep. Mycelium’s lease / poll / hard-block path is the operator-side guard for that window — see [Resolution gates](#resolution-gates).

```bash
pip install 'mycelium-runtime[langgraph]'  # Python 3.10+; automatic runtime IDs
mycelium init                  # on-ramp scaffold (transition + one ledgered tool)
mycelium init --full           # reference scaffold (all guards; fill TODOs)
mycelium demo                  # see the bug and the fix (no LangGraph required)
```


```yaml
tools:
  subagent_task:
    callable: my_agent.tools:subagent_task
    side_effect_class: non_idempotent_mutate
```

```bash
mycelium run --config mycelium.yaml -- python -m my_agent
```

In v1.11.0, the default `mycelium init` YAML enables `integrations.langgraph`. LangGraph's
`ToolNode` / `create_agent` injects a hidden `ToolRuntime`, and Mycelium maps
its `tool_call_id`, thread, run, and node into the transition key. No
`tool_call_id` parameter is needed on your function. Explicit IDs still win;
custom tool executors may continue passing them manually.

New in v1.12.0, `mycelium run` wraps all configured tool/task callable paths before application
startup and then replaces itself with the child Python process. Existing
`@config.apply`, `@config.apply_task`, and `config.instrument` flows remain
supported for explicit code-level control.

## What else it does

| Problem | What Mycelium does |
|---------|-------------------|
| **Stale or broken context** | TTL cache, message repair, history limits; agent sees fresh, valid data |
| **Bad or unauthorized tool calls** | Validate inputs/outputs, allowlists, scoped paths; block before execution |
| **Duplicate side effects on retry** | Transition envelope (v1.3+): `side_effect_class`, terminal outcomes, resolution **gates** (`POLL` / `REPAIR` / `SOFT_BLOCK` / `HARD_BLOCK`), `external_operation_ref` + `Reconciler`, ledgers, signed receipts |

Framework-agnostic. Raw message lists and plain Python functions (LangGraph, CrewAI, OpenAI tool loops, etc.).

## Install

**Requires Python 3.10+** (3.11+ recommended).

```bash
pip install mycelium-runtime
pip install 'mycelium-runtime[langgraph]'  # optional automatic LangGraph IDs
mycelium init              # on-ramp: duplicate-tool fix → ./mycelium.yaml
mycelium init --full       # reference: every guard section (not the default)
mycelium init --minimal    # smaller multi-guard scaffold
mycelium demo              # terminal demo of langgraph#7417
```

## Quickstart: stale context & broken transcripts

```python
from mycelium import protect, Session

@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

async def handle_request(customer_id: str):
    async with Session():
        return await fetch_customer(customer_id=customer_id)
```

Sync tools (CrewAI, Smolagents):

```python
from mycelium import protect_sync, Session

@protect_sync(entity_param="customer_id", ttl=60)
def fetch_customer(customer_id: str) -> dict:
    return db.get(customer_id)

with Session():
    customer = fetch_customer(customer_id="c1")
```

## What `@protect` / `protect_sync` / `Session` do

- `@protect` / `protect_sync`: TTL cache with per-entity keys; auto-refetch when stale; clear on error
- `Session`: one cache per agent run; use in production to prevent cross-request leakage

## MessageValidator

Run before each LLM call to catch broken transcripts:

```python
from mycelium import MessageValidator

messages = MessageValidator().repair(messages)  # auto-fix what it can
# or
messages = MessageValidator().validate(messages)  # raise on first issue
```

Catches orphan tool results, duplicate tool-call IDs, invalid roles, and related serialization bugs.

## HistoryGuard

Run before each LLM call to catch oversized or corrupted history:

```python
from mycelium import HistoryGuard

guard = HistoryGuard(max_tokens=100_000)
messages = guard.validate(messages)
guard.check_for_drops(processed_messages)  # after framework trimming
```

Raises on token overflow, message count limits, duplicate turns, and silent message drops.

## Quickstart: tool boundaries

```python
from mycelium import bounded, ToolRegistry, ToolRunner

FETCH_CUSTOMER_SCHEMA = {
    "customer_id": {"type": "string", "required": True, "pattern": r"^c\d+$"},
}

CUSTOMER_RECORD_SCHEMA = {
    "customer_id": {"type": "string", "required": True},
    "name": {"type": "string", "required": True},
}

registry = ToolRegistry(allowed=["fetch_customer"])

@registry.register
@bounded(
    schema=FETCH_CUSTOMER_SCHEMA,
    output_schema=CUSTOMER_RECORD_SCHEMA,
    allowed_paths=["/workspace/src/"],
)
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

runner = ToolRunner(registry=registry)
result = await runner.call(fetch_customer, customer_id="c1")
```

Sync tools:

```python
from mycelium import bounded_sync

@bounded_sync(schema=FETCH_CUSTOMER_SCHEMA)
def fetch_customer(customer_id: str) -> dict:
    return db.get(customer_id)
```

Field spec keys: `type` (`string`, `integer`, `number`, `boolean`), `required`, `pattern`, `min_length`, `max_length`. You pass plain dicts; Mycelium validates internally; no Pydantic imports in your code.

## What `@bounded` / `bounded_sync` do

- `@bounded` / `bounded_sync`: validate tool args against your field spec **before** the function runs
- `output_schema`: validate the return value **after** the function runs; bad results are not propagated
- `allowed_paths` / `entity_pattern`: user-defined scope gates (path prefixes, entity ID format)
- On failure, raises `ToolBoundaryError` with `llm_message` for the agent loop; does not retry by itself

## ToolRegistry

Run before dispatch to enforce which tools this agent may call:

```python
from mycelium import ToolRegistry

registry = ToolRegistry(allowed=["search_docs", "summarize"])
registry.validate_call("fetch_customer")  # raises ToolBoundaryError
```

Blocks calls to tools outside the developer-defined allowlist.

## ToolRunner

Run around `@bounded` tools when you want automatic retries:

```python
from mycelium import ToolRunner

runner = ToolRunner(registry=registry, max_llm_retries=2, max_tool_retries=3)

result, messages = await runner.run_with_llm_retry(
    fetch_customer,
    messages=messages,
    tool_call_id="call_1",
    kwargs={"customer_id": "c1"},
    invoke_llm=llm.ainvoke,
    parse_tool_kwargs=extract_tool_args,
)
```

- Input, allowlist, and scope failures → append tool error to messages → LLM retry
- Output failures → retry the tool up to `max_tool_retries` → then LLM retry
- Raises `ToolBoundaryExhaustedError` when retries are used up

## Quickstart: idempotency & audit receipts (v1.3 transition envelope)

Stop duplicate payments, emails, and API calls when the framework retries. Five **effect-semantic** `side_effect_class` values plus optional `spendability` (`multi_use` / `single_use` / `non_replayable`): reads poll in-flight duplicates; mutating tools hard-block ambiguous states instead of blind re-execute.

### Tool-level idempotency

```python
from mycelium import ledger_sync
from mycelium.transition import SideEffectClass, ToolTransitionBinding

binding = ToolTransitionBinding.for_tool(
    agent_id="payment-agent",
    policy_version="2026.07.1",
    side_effect_class=SideEffectClass.KEYED_MUTATE,
)

@ledger_sync(transition_binding=binding)
def send_payment(amount: float, recipient: str) -> dict:
    return gateway.charge(amount, recipient)

# Same logical call executes only once.
send_payment(amount=100.0, recipient="acct_123", tool_call_id="call_abc")
send_payment(amount=100.0, recipient="acct_123", tool_call_id="call_abc")
```

Or wire from YAML (recommended):

```yaml
integrations:
  langgraph:
    enabled: true

transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"
  lease_ttl: 3600

action_ledger:
  storage: file
  path: ./mycelium-ledger.json
  tools: [send_payment, search_docs]

tools:
  send_payment:
    callable: my_agent.tools:send_payment
    side_effect_class: keyed_mutate
    # spendability defaults to single_use for keyed_mutate
    retry_permission: manual_reconciliation_required
  search_docs:
    callable: my_agent.tools:search_docs
    side_effect_class: read
    # spendability defaults to multi_use for read
```

When enabled, command mode or `@config.apply` adds a hidden keyword-only
`runtime: ToolRuntime` parameter. LangGraph treats it as a trusted injected
argument (not an LLM-visible tool input), while the original function remains
unchanged. Calls outside LangGraph still work. This requires
`mycelium-runtime[langgraph]` and LangGraph's `ToolNode` or `create_agent`;
custom executors must pass IDs themselves.

For zero-touch instrumentation, launch with:

```bash
mycelium run --config mycelium.yaml -- python -m my_agent
```

Every non-noop tool/task must declare a unique `callable: module:function`.
Targets are imported and validated before the application entrypoint runs;
missing/non-callable targets and partial Mycelium wrappers stop startup. A
fully configured `@config.apply` or `@config.apply_task` target is skipped.
Only the current Python interpreter is accepted, and `-E`, `-I`, and `-S` are
rejected because they disable the startup hook. Keep target modules import-safe.
Code that registers a function inside its own module before that import
completes cannot be retroactively updated; move registration to the entrypoint
or use explicit instrumentation for that target.

Async tools:

```python
from mycelium import ledger

@ledger()
async def send_payment(amount: float, recipient: str) -> dict:
    return await gateway.charge(amount, recipient)
```

## What `@ledger` / `ledger_sync` do

- Record every tool invocation in a durable `ActionLedger`
- Deduplicate retries and redispatches via a rich **transition key** (scope + tool + args + `side_effect_class` + policy), not only `tool_call_id`
- Resolve redispatches through **gates** (see [Resolution gates](#resolution-gates)) instead of re-running blindly
- Persist failed attempts with **terminal outcomes** (`FAILED_BEFORE_EFFECT`, `FAILED_AFTER_EFFECT`, `UNKNOWN`, `EXPIRED`, etc.) for audit and reconciliation

### Resolution gates

**Invariant:** do not redispatch unless the previous transition is **proven terminal** (e.g. `COMPLETED` → return stored) or **safely recoverable** (poll in-flight, soft-block/retry a read `UNKNOWN`, or reconcile via `external_operation_ref`). Otherwise hard-block — never blind re-execute a side effect.

Each duplicate dispatch is classified to a gate. Read-only and side-effecting tools use different resolvers.

| Gate | Typical trigger | What happens |
|------|-----------------|--------------|
| `ALLOW` | no prior transition, or policy permits retry (e.g. `FAILED_BEFORE_EFFECT` + same provider key) | tool runs |
| `RETURN` | `COMPLETED` | return stored result — no re-execution |
| `POLL` | `IN_FLIGHT` with valid lease (`LeaseValidity.HELD`) | wait for the other worker |
| `RECLAIM` | read-only `EXPIRED` / `FAILED_*` | take over stale lease and run |
| `REPAIR` | incomplete durable key / boundary / terminal (healable) | fix record, re-resolve — **no** second side effect |
| `SOFT_BLOCK` | read-only `UNKNOWN` / `BLOCKED` only | **retry by default** (safe — reads don't spend); opt into deferral with `defer_read_only_unknown=True` → `LedgerSoftBlockError` |
| `HARD_BLOCK` | ambiguous mutating transition | stop; run `Reconciler` when `external_operation_ref` is present, else fail-closed |

**Public transition-sufficiency language:** #7417-style discussions often use four words — `ALLOW` / `REPAIR` / `SOFT_BLOCK` / `HARD_BLOCK` (sometimes `BLOCK`). Mycelium implements that set and adds finer internals:

| Public | Mycelium | Notes |
|--------|----------|-------|
| `ALLOW` | `ALLOW` | run / safe retry |
| `REPAIR` | `REPAIR` | heal durable context; owner `renew_lease()` for a live lease |
| `SOFT_BLOCK` | `SOFT_BLOCK` | read-only defer / safe retry |
| `HARD_BLOCK` / `BLOCK` | `HARD_BLOCK` | stop; reconcile if ref present |
| *(must not run again)* | `RETURN` / `POLL` | already done, or wait on a held lease |
| *(read reclaim)* | `RECLAIM` | take over an expired read lease and run |

Public `BLOCK` ≈ Mycelium `HARD_BLOCK`. `RETURN` and `POLL` are also “do not execute again” under the richer internal taxonomy — use the four public words with platforms; use the full table when implementing or debugging.

**Lease validity (v1.10.0):** `lease_until` is resolution metadata — **not** part of `transition_key` (so renewals do not fork identity). Before reclaim/retry, resolution classifies the window via `LeaseValidity` (`HELD` → poll, `EXPIRED` → reclaim or hard-block by class, `UNBOUNDED` → no TTL). During long work call `renew_lease()` inside the ledgered tool to keep peers on `POLL`.

**`REPAIR` (v1.13.0):** when the durable record is incomplete but healable (missing `idempotency_key`, invalid/missing `side_effect_boundary` or `terminal_outcome`, or status/terminal drift), claim loops call `repair_transition()` then re-resolve. A held in-flight lease is still `POLL` for peers; the owner extends via `renew_lease()` (not a second execute).

**Read-only** (`side_effect_class: read`): poll, reclaim, retry failed-before-effect, soft-block on ambiguous `UNKNOWN`/`BLOCKED`.

**Mutating** (payment, email, subagent, irreversible, …): return completed, poll in-flight, hard-block ambiguity. For **`EXPIRED + not_crossed`**, the gate is `HARD_BLOCK` until a reconciler proves the effect never ran — see [Stale lease + reconcile](#stale-lease--reconcile-exired--not_crossed).

### Transition envelope fields

Six fields decide whether an unresolved prior execution is merely **wasteful** (safe to retry/poll) or **unsafe** (must not re-run). Priority order:

| # | Field | Role |
|---|-------|------|
| 1 | `side_effect_class` | What kind of effect (`read`, `keyed_mutate`, `non_idempotent_mutate`, …) |
| 2 | `spendability` | How many times the same intent may spend (`multi_use` / `single_use` / `non_replayable`) |
| 3 | `side_effect_boundary` | Whether the external call was crossed (`not_crossed` / `maybe_crossed` / `crossed`) |
| 4 | `terminal_outcome` | Where the prior attempt ended (`IN_FLIGHT`, `COMPLETED`, `UNKNOWN`, `EXPIRED`, …) |
| 5 | `external_operation_ref` | Provider handle for read-only reconcile (id or idempotency key) |
| 6 | `retry_permission` | Whether automatic retry is allowed (and same-key enforcement when opted in) |

**Invariant:** for a given tool class, the fields that class **requires** must already be **supported and recorded** on the transition before a redispatch is treated as a safe retry. Reads need a lighter set (class + terminal + lease). Payment / write / email / subagent need spendability, boundary, terminal outcome, and usually an external receipt/ref — without them, a second dispatch is an **unsupported second transition**, not a retry.

Also on the durable record: `transition_key`, `idempotency_key`, `owner`, `lease_until`, `receipt_ref`.

### Side-effect classes

| Class | Typical use | Duplicate handling |
|-------|-------------|-------------------|
| `read` | search, fetch | poll / reclaim / retry; `SOFT_BLOCK` on `UNKNOWN` |
| `idempotent_mutate` | upsert / set status | retry if boundary not crossed |
| `keyed_mutate` | Stripe-style create/charge | retry only with same provider key |
| `non_idempotent_mutate` | send email, spawn subagent | hard-block on ambiguity |
| `irreversible` | wire / on-chain burn | hard-block → human |

Legacy aliases (`read_only`, `payment`, `subagent`, …) still parse. Set per tool in YAML with `side_effect_class`. Required when `transition:` is configured and the tool is ledgered.

### Spendability

Orthogonal to `side_effect_class` — how many times the same intent may produce an effect:

| Value | Meaning | Default for |
|-------|---------|-------------|
| `multi_use` | may produce effects again | `read`, `idempotent_mutate` |
| `single_use` | one effect; COMPLETED returns stored result | `keyed_mutate`, `non_idempotent_mutate` |
| `non_replayable` | ambiguity → hard-block / reconcile | `irreversible` |

Override with `spendability:` only when the class default is wrong for your tool. Same transition key always returns the COMPLETED result; a deliberate re-spend needs a new key.

### Marking the side-effect boundary (`side_effect()`)

By default a failing tool is recorded as `FAILED_BEFORE_EFFECT` — safe to retry. But if the external call already happened (e.g. the charge succeeded and then response parsing threw), that classification is wrong. Wrap the external operation in `side_effect()` so the ledger knows where the point of no return is:

```python
from mycelium import ledger_sync, side_effect

@ledger_sync(transition_binding=binding)
def send_payment(amount: float, recipient: str) -> dict:
    validate(amount, recipient)          # boundary: not_crossed
    with side_effect():                  # -> maybe_crossed before the call
        resp = gateway.charge(amount, recipient)   # -> crossed on clean exit
    return parse(resp)
```

The boundary drives failure classification and only ever moves forward (`not_crossed → maybe_crossed → crossed`):

| Boundary when it fails/crashes | Terminal outcome | Redispatch |
|--------------------------------|------------------|------------|
| `not_crossed` (before the block) | `FAILED_BEFORE_EFFECT` | retry if policy allows |
| `maybe_crossed` (inside the block / crash) | `UNKNOWN` | hard-block → reconcile |
| `crossed` (clean exit, or `mark_crossed()`) | `FAILED_AFTER_EFFECT` | hard-block |

Because `maybe_crossed` is written durably *before* the call, a process crash mid-call leaves the entry ambiguous and a redispatch hard-blocks instead of double-spending. For finer control use `mark_maybe_crossed()` / `mark_crossed()` directly. Works the same inside `async` tools.

### Read-only `SOFT_BLOCK` (v1.9.0)

When a read-only tool ends in `UNKNOWN` or `BLOCKED`, the resolver returns `SOFT_BLOCK` — not a terminal stop. Re-running a read is always safe, so the default is **retry** (reset to a fresh in-flight claim and run once more). For expensive reads, opt into deferral:

```python
from mycelium import ledger_sync, LedgerSoftBlockError

@ledger_sync(transition_binding=read_binding, defer_read_only_unknown=True)
def search_docs(query: str) -> dict:
    ...
```

With `defer_read_only_unknown=True`, ambiguous read-only states raise `LedgerSoftBlockError` so the caller can retry later (cost-dependent). Side-effecting tools never use `SOFT_BLOCK`; they use `HARD_BLOCK` / reconcile.

### Recording the provider handle (`record_external_operation()`)

When a side-effecting tool talks to a provider, record the provider's operation handle — its returned id (Stripe `pi_...`, a message id, a run id) or the idempotency key you sent — so an ambiguous transition can later be **reconciled** against the provider instead of parked for a human:

```python
from mycelium import ledger_sync, side_effect, record_external_operation

@ledger_sync(transition_binding=binding)
def send_payment(amount, recipient):
    with side_effect():
        intent = gateway.charge(amount, recipient, idempotency_key=key)
        record_external_operation(intent.id)   # durable on the ledger entry
    return intent
```

The ref is stored on the entry (`external_operation_ref`) across all backends and shown in the hard-block message. Prefer recording the **idempotency key before the call** for keyed providers — it survives a crash mid-call, unlike a returned id.

`external_operation_ref` is the **handle** for provider lookup; it is not proof by itself. Proof comes from the reconciler's read-only query (below).

### Reconciling automatically (`Reconciler`)

Instead of parking an ambiguous transition for a human, give the ledger a **read-only** `Reconciler` that asks the provider "did operation X actually complete?" using the recorded ref. It runs only when a side-effecting transition would otherwise hard-block *and* a ref is present:

```python
from mycelium import ledger_sync, Reconciler, ReconcileResult

class StripeReconciler:  # read-only: never charges, never retries
    def reconcile(self, entry) -> ReconcileResult:
        pi = stripe.PaymentIntent.retrieve(entry.external_operation_ref)
        if pi.status == "succeeded":
            return ReconcileResult.completed(pi)
        if pi.status in ("canceled", "requires_payment_method"):
            return ReconcileResult.not_executed()
        return ReconcileResult.unknown()

@ledger_sync(transition_binding=binding, reconciler=StripeReconciler())
def send_payment(amount, recipient):
    with side_effect():
        intent = gateway.charge(amount, recipient, idempotency_key=key)
        record_external_operation(intent.id)
    return intent
```

| Reconcile result | What happens on redispatch |
|------------------|-----------------------------|
| `COMPLETED` | returns the reconciled result — the tool body does **not** run again |
| `NOT_EXECUTED` | the tool is allowed to run **exactly once** more |
| `UNKNOWN` | hard-blocks, exactly as if no reconciler were set |

Reconciliation is **fail-closed**: no ref, no reconciler, or a reconciler that raises/times out all resolve to a hard-block — an exception in the reconciler never propagates. Async tools can implement `reconcile_async`; the async claim path prefers it and falls back to `reconcile`. Wire a reconciler via `@ledger` / `@ledger_sync` or `ActionLedger(reconciler=...)`.

### Stale lease + reconcile (`EXPIRED + not_crossed`)

When a worker dies or a lease expires while a side-effecting tool is still `IN_FLIGHT`, the transition becomes `EXPIRED`. Resolution depends on boundary and class:

| Situation | Gate | Reclaim? |
|-----------|------|----------|
| `EXPIRED` + `maybe_crossed` / `crossed` | `HARD_BLOCK` | no — effect may have happened |
| `EXPIRED` + `not_crossed`, strict class, **no** `external_operation_ref` | `HARD_BLOCK` | no — not provable |
| `EXPIRED` + `not_crossed` + ref + reconciler → `NOT_EXECUTED` | reconcile → fresh claim | yes — provider proves effect never ran |
| `EXPIRED` + `not_crossed` + ref + reconciler → `COMPLETED` | `RETURN` | no — return stored/reconciled result |
| `EXPIRED` + `not_crossed`, `multi_use` + `SAFE_RETRY` (e.g. idempotent read/write) | `ALLOW` | yes — reclaim without reconcile |

If a duplicate worker is **polling** an in-flight transition and the lease expires mid-poll, the poll loop returns (v1.9.2) so the claim path can reconcile instead of hard-blocking immediately.

Record `external_operation_ref` early (ideally the idempotency key before the provider call) so stale-lease and `UNKNOWN` cases can be resolved automatically instead of parking for a human.

### Enforcing the same provider idempotency key (`provider_idempotency_key_param`)

`retry_only_with_same_provider_idempotency_key` (the default for `keyed_mutate`) means "a retry is safe *only if* it reuses the same provider idempotency key so the provider dedupes." By default Mycelium trusts you to reuse it. To have Mycelium **enforce** it, declare which kwarg carries the key:

```yaml
tools:
  send_payment:
    side_effect_class: keyed_mutate          # retry_only_with_same_provider_idempotency_key
    provider_idempotency_key_param: idempotency_key
```

or in code: `ToolTransitionBinding.for_tool(..., provider_idempotency_key_param="idempotency_key")`.

With it declared, on a retry of a transition that failed before the effect:

| Incoming key vs stored key | Gate |
|----------------------------|------|
| same key | `ALLOW` (retry proceeds; provider dedupes) |
| different key | `HARD_BLOCK` (would risk a second, undeduped effect) |
| missing on either side | `HARD_BLOCK` |

The declared key is excluded from the transition-key fingerprint, so a retry that swaps the key still resolves to the *same* transition and is caught rather than silently starting a new one. This is **opt-in**: tools that don't declare the param keep the previous cooperative behavior.

Storage backends:

| Backend | Use case | YAML `storage` |
|---------|----------|----------------|
| `memory` | Single process, tests | `memory` (default) |
| `file` | Local dev, single host (`fcntl` lock) | `file` + `path` |
| `redis` | Multi-worker, in-flight TTL | `redis` + `url` or `url_env` |
| `postgres` | Audit/compliance, durable SQL | `postgres` + `dsn` or `dsn_env` |

```python
from mycelium import ActionLedger, FileLedgerStorage, InMemoryLedgerStorage
from mycelium import RedisLedgerStorage, PostgresLedgerStorage

ledger = ActionLedger(storage=InMemoryLedgerStorage())
ledger = ActionLedger(storage=FileLedgerStorage("./mycelium-ledger.json"))
ledger = ActionLedger(storage=RedisLedgerStorage("redis://localhost:6379/0"))
ledger = ActionLedger(storage=PostgresLedgerStorage("postgresql://localhost/mycelium"))
```

Optional extras: `pip install 'mycelium-runtime[redis]'` or `pip install 'mycelium-runtime[postgres]'`.

## Quickstart: task-level idempotency

Stop entire tasks from re-running on framework-level retries:

```python
from mycelium import task_ledger_sync

@task_ledger_sync()
def process_invoice(invoice_id: str) -> dict:
    customer = fetch_customer(customer_id=...)
    payment = send_payment(...)
    return {"invoice_id": invoice_id, "status": "paid"}

# Framework retries the task with the same task_id
process_invoice(invoice_id="inv-42", task_id="invoice-42")  # executes
process_invoice(invoice_id="inv-42", task_id="invoice-42")  # returns stored result
```

Use `id_from` to derive the task id from business keys automatically:

```python
@task_ledger_sync(id_from=["invoice_id"])
def process_invoice(invoice_id: str, amount: float) -> dict:
    ...

# Both calls map to the same task id because invoice_id is the same.
process_invoice(invoice_id="inv-42", amount=100.0)
process_invoice(invoice_id="inv-42", amount=200.0)  # returns first result
```

### Correction retries

If a completed task produced a bad result and the LLM/agent needs to re-attempt it, use a **new task id**. The framework will normally generate fresh tool call ids for the new attempt, so the task re-executes cleanly.

```python
r1 = process_invoice(invoice_id="inv-42", task_id="invoice-42-attempt-1")  # bad result
r2 = process_invoice(invoice_id="inv-42", task_id="invoice-42-attempt-2")  # fresh attempt
```

## YAML configuration

Separate YAML sections per guard type. Global ledger settings inherit into tools/tasks
so you do not repeat storage paths on every function.

**Minimum integration (3 steps):**

```yaml
# mycelium.yaml: global sections (configure once)
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"
  lease_ttl: 3600

action_ledger:
  storage: file
  path: ./mycelium-ledger.json
  tools: [send_payment, search_docs]

task_ledger:
  storage: file
  path: ./mycelium-task-ledger.json
  tasks: [process_invoice]

state_flush:
  storage: file
  path: ./mycelium-state.json

audit_receipt:
  signing_key_env: MYCELIUM_SIGNING_KEY
  storage: file
  path: ./mycelium-receipts.jsonl

# Per-tool: side_effect_class + schemas
tools:
  fetch_customer:
    callable: my_agent.tools:fetch_customer
    side_effect_class: read
    protect: {entity_param: customer_id, ttl: 60}
    bounded:
      schema:
        customer_id: {type: string, required: true, pattern: "^c\\d+$"}

  send_payment:
    callable: my_agent.tools:send_payment
    side_effect_class: keyed_mutate
    bounded:
      schema:
        amount: {type: number, required: true}
        recipient: {type: string, required: true}

  search_docs:
    callable: my_agent.tools:search_docs
    side_effect_class: read

tasks:
  process_invoice:
    callable: my_agent.tasks:process_invoice
    ledger: true
    id_from: [invoice_id]

registry:
  auto: true                     # allowlist = all configured tools

history_guard:
  max_tokens: 100000

message_validator:
  enabled: true
```

```bash
# Zero-touch mode: callable paths above select the functions.
mycelium run --config mycelium.yaml -- python -m my_agent
```

Or instrument explicitly in Python:

```python
from mycelium import load_config
import my_tools

config = load_config("mycelium.yaml")
tools = config.instrument(my_tools)   # one call wraps tools + tasks

with config.run(thread_id):
    messages = config.prepare_messages(messages)  # message validation + state flush
    ...
```

`ledger: true` inherits from `action_ledger` / `task_ledger`. When `audit_receipt`
is configured with `auto: true` (default), all ledgered tools/tasks get signed
receipts automatically. Set `transition.agent_id` for receipt identity (replaces
`audit_receipt.agent_id` from v1.2).

Configs without `transition:` keep v1.2 ledger behavior. See [CHANGELOG](../CHANGELOG.md) for breaking changes.

Legacy per-tool style still works. Start with `mycelium init`; use `mycelium init --full` for the all-guards reference template.

---

## For contributors (repo layout)

Clone the GitHub repo to run proofs and tests. PyPI installs only the `mycelium` package.

```bash
git clone https://github.com/mycelium-labs/mycelium.git
cd mycelium/sdk && pip install -e ".[dev]"
pytest tests/ -v
```
