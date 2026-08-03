# Mycelium runtime

[![PyPI version](https://img.shields.io/pypi/v/mycelium-runtime.svg?cacheSeconds=60&release=1.24.0)](https://pypi.org/project/mycelium-runtime/)
[![Python](https://img.shields.io/pypi/pyversions/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)

Current package: **mycelium-runtime v1.24.0** (SQLite ledger backend + AF-002 failure-case pack + AF-007 completion contract + state-authority execution gate + AF-003 loop guard + outcome telemetry / DTTR + fail-closed Gmail sent-log reconciler + webhook event-dedupe recipe + atomicity contract + CAS backends + owner fencing + worker-death signal + operator release + `REPAIR` gate + lease auto-renew + transition envelope).

## One painful bug → a few lines of config

**LangGraph Cloud redispatches a long tool call while the first is still running.** Both complete. You pay twice. Side effects run twice. [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417)

Mycelium’s answer is a **transition envelope**, not “idempotency key + cached result” alone: classify the tool (**side-effect class**), hold an execution **lease** while work is in flight (auto-extended for long `@ledger` tools), record **terminal state** (`IN_FLIGHT` / `COMPLETED` / `UNKNOWN` / …), and **hard-block** (or reconcile) when a mutating redispatch would be unsafe. Same key while in-flight → poll; completed → return stored; ambiguous payment-class → stop.

On LangGraph Cloud, long tool calls can be redispatched on the order of **~180s**, aligned with the platform’s **`BG_JOB_HEARTBEAT`** sweep. Mycelium’s lease / auto-renew / poll / hard-block path is the operator-side guard for that window — see [Resolution gates](#resolution-gates).

```bash
pip install 'mycelium-runtime[langgraph]'  # Python 3.10+; automatic runtime IDs
mycelium init                  # on-ramp scaffold (transition + one ledgered tool)
mycelium init --full           # reference scaffold (all guards; fill TODOs)
mycelium demo                  # feature tour (envelope, lease, hard-block, repair, reconcile, release)
mycelium demo --redis          # optional 2-worker Redis proof
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

## What it does

Mycelium sits between your agent loop and your tools (after the LLM returns `tool_calls`):

| | Problem | What Mycelium does |
|---|---------|-------------------|
| **Core** | **Duplicate side effects on retry** | Transition envelope: classify tools, durable transition key, lease (+ auto-renew while `@ledger` runs), terminal state, resolution **gates** (`POLL` / `REPAIR` / `SOFT_BLOCK` / `HARD_BLOCK`), `external_operation_ref` + `Reconciler`, ledgers, signed receipts |
| **Core** | **Transition envelope fields** | `side_effect_class` → `spendability` → `side_effect_boundary` → `terminal_outcome` → `external_operation_ref` → `retry_permission` — same system as above; payment/write needs the heavier set |
| **Opt-in** | **Infinite action loops (AF-003)** | `loop_guard:` — action-hash streak across *new* `tool_call_id`s; soft (`ToolBoundaryError`) then hard (`LedgerHardBlockError`); operator `mycelium loops release` |
| **Opt-in** | **Premature termination (AF-007)** | `completion:` — host checklist; unmarked **required** → refuse terminal; unmarked **optional** → warn and allow; `complete_run` / graph END / final-message adapters |
| **Opt-in** | **Superseded state / state authority** | `state_authority:` — freeze `state_ref` at decide time; compare to host canonical ref before claim; mismatch blocks even when `tool_call_id` is new |
| **Opt-in** | **Stale or broken context** | TTL cache (`@protect` / `Session`); optional `MessageValidator` / `HistoryGuard` you call before the next LLM turn |
| **Opt-in** | **Bad tool calls** | `@bounded` input/output/scope checks; optional `ToolRegistry` allowlist — block before the tool runs |

`mycelium init` / `mycelium run` center on the core path. Context and tool-boundary guards are available when you configure them.

Framework-agnostic. Raw message lists and plain Python functions (LangGraph, CrewAI, OpenAI tool loops, etc.).

## What Mycelium does not do

Mycelium is an **embeddable transition envelope at the tool boundary** — classify → claim/lease → gate (`RETURN` / `POLL` / `REPAIR` / `HARD_BLOCK` / …) → optional reconcile — for LangGraph, CrewAI, or plain Python, via YAML + decorators or manual claim/complete. It is not a full agent platform and deliberately stays out of adjacent lanes:

| Not this | That lane | What Mycelium does instead |
|---|---|---|
| Approvals inbox / policy-builder UI | Approval & governance products (DashClaw / ThumbGate) | Hard-block + operator `release` (CLI/API); wire your own approver upstream |
| Hosted traces & dashboards | Observability (Langfuse / LangSmith) | Optional local `OutcomeEmitter` / DTTR — opt-in telemetry, not a hosted identity |
| On-chain audit trails | Separate “trails” / Argentum-style products | Durable ledger + optional provider reconcile / signed receipts — runtime/ledger anchors, not chain anchors |
| Generic webhook/SaaS hub | Event buses / claim APIs | The same ledger *can* key on provider event ids; the wedge stays agent-tool redispatch |
| Fix bad reasoning / rewind runs | Evals, memory, recovery tools | Stops unsafe **re-execution** of side effects at the tool boundary |

**Compose:** use Mycelium *under* an approval layer and *beside* a tracer if you want all three — they don't replace each other. Layers shouldn't trust each other.

## Install

**Requires Python 3.10+** (3.11+ recommended).

```bash
pip install mycelium-runtime
pip install 'mycelium-runtime[langgraph]'  # optional automatic LangGraph IDs
mycelium init              # on-ramp: duplicate-tool fix → ./mycelium.yaml
mycelium init --full       # reference: every guard section (not the default)
mycelium init --minimal    # smaller multi-guard scaffold
mycelium demo              # feature tour: unguarded vs ledgered + gates / hard-block / release
mycelium demo --redis      # optional Cloud-style 2-worker Redis proof
```

## Quickstart: stale context & broken transcripts (opt-in)

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

## Quickstart: tool boundaries (opt-in)

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

## Quickstart: idempotency & audit receipts (core — transition envelope)

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
  # lease_renew_interval: 1200   # default = lease_ttl/3; 0 disables auto-renew

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

### Manual integration (claim → execute → complete)

Prefer `@ledger_sync` / `@ledger`, YAML + `mycelium run`, or `@config.apply` — those wrap the tool and run the two phases for you. Use the explicit API only when you already own the tool runner (custom loop, PROCEED/SKIP-style host) and cannot take a decorator.

Same ledger, same gates, same hard-block rules. You call claim and complete yourself:

```python
from mycelium import (
    ActionLedger,
    FileLedgerStorage,
    LedgerHardBlockError,
    TerminalOutcome,
    execution_scope,
)
from mycelium.transition import SideEffectClass, ToolTransitionBinding, TransitionScope

ledger = ActionLedger(storage=FileLedgerStorage("./mycelium-ledger.json"))
binding = ToolTransitionBinding.for_tool(
    agent_id="payment-agent",
    policy_version="2026.07.1",
    side_effect_class=SideEffectClass.KEYED_MUTATE,
)

def send_payment(amount: float, recipient: str, *, tool_call_id: str) -> dict:
    args = (amount, recipient)
    kwargs = {"tool_call_id": tool_call_id}
    request_id = ledger.derive_request_id(
        "send_payment", args, kwargs, transition_binding=binding
    )
    with execution_scope(TransitionScope(thread_id="t1", run_id="r1", node="tools")):
        try:
            entry = ledger.claim_side_effecting(
                request_id, "send_payment", args, kwargs, binding
            )
        except LedgerHardBlockError:
            # Ambiguous mutate — reconcile / operator release; do not re-run.
            raise

        # SKIP / RETURN: already completed — replay stored result, no second send.
        if entry.terminal_outcome == TerminalOutcome.COMPLETED.value:
            return entry.result

        # PROCEED: we hold IN_FLIGHT — run the side effect once, then settle.
        try:
            result = gateway.charge(amount, recipient)
        except Exception as exc:
            ledger.fail(request_id, exc, failed_after_effect=False)
            raise
        ledger.complete(request_id, result)
        return result
```

| Step | Meaning |
|------|---------|
| `claim_side_effecting(...)` | May I run? Resolves gates (`RETURN` / `POLL` / `HARD_BLOCK` / …). Raises on hard-block. |
| `COMPLETED` → return `entry.result` | Partner-facing **SKIP** — already done. |
| Else run body + `complete(...)` | Partner-facing **PROCEED** then settle. |
| `fail(...)` | Settle a failure; use `failed_after_effect=True` if the provider may have accepted. |

The `side_effect()` / `record_external_operation()` boundary helpers only take effect inside `@ledger` / `@ledger_sync` tool bodies; in this manual path they are ignored. Durability still holds from the claim itself: if the process dies after claiming, the entry expires and a redispatch hard-blocks rather than re-running.

`mycelium init` / `mycelium run` always use the wrapper path — there is no YAML switch for manual claim/complete. For long tools claimed outside the decorator, call `renew_lease(request_id)` (or pass `lease_renew_interval` when you build the ledger) so peers keep polling instead of reclaiming mid-flight. See [Resolution gates](#resolution-gates).

### Webhook event dedupe (optional)

If you already use Mycelium to guard agent tools, you can also claim inbound
provider events the same way. This is an **optional adjacent recipe** — agent
tools stay the primary use case, and it is not a general webhook platform.

Inbound providers deliver **at-least-once** (Stripe, GitHub, Twilio all retry
on non-2xx). Claim the **provider event id** through the same `ActionLedger`
and you get **at-most-once handler side effects for that event id**: the first
delivery does the work and `complete`s; a redelivery hits the `RETURN`/SKIP
path and returns `200` without re-running the side effect.

Key the transition on the **provider event id** — Stripe `event.id`, GitHub
`X-GitHub-Delivery`, Twilio message/event SID — not the whole payload, not the
provider's *response* id, and not Stripe's `Idempotency-Key` *request* header
(that header dedupes requests you send *to* Stripe; it is unrelated to inbound
delivery). Because the event id is the only arg in the fingerprint, a
redelivery with slightly different payload bytes still resolves to the same
transition. Pin `agent_id` and `policy_version` across deploys so the key
stays stable after a release.

Verify the provider signature **before** claiming. Then claim, work once, and
settle — the same manual API as above. On `HARD_BLOCK`, fail closed (reconcile
or use the operator-release path), never re-run blindly:

```python
from mycelium import (
    ActionLedger,
    FileLedgerStorage,
    LedgerHardBlockError,
    TerminalOutcome,
)
from mycelium.transition import SideEffectClass, ToolTransitionBinding

ledger = ActionLedger(storage=FileLedgerStorage("./webhook-events.json"))
binding = ToolTransitionBinding.for_tool(
    agent_id="webhook-worker",
    policy_version="2026.08.1",
    side_effect_class=SideEffectClass.KEYED_MUTATE,
)

def handle_event(tool: str, event_id: str, do_work) -> int:
    args, kwargs = (event_id,), {}
    request_id = ledger.derive_request_id(tool, args, kwargs, transition_binding=binding)
    try:
        entry = ledger.claim_side_effecting(request_id, tool, args, kwargs, binding)
    except LedgerHardBlockError:
        return 409                          # HARD_BLOCK: reconcile / operator release
    if entry.terminal_outcome == TerminalOutcome.COMPLETED.value:
        return 200                          # SKIP: already handled this event id
    try:
        result = do_work()                  # the side effect, once
    except Exception as exc:
        ledger.fail(request_id, exc, failed_after_effect=False)
        return 500
    ledger.complete(request_id, result)
    return 200                              # PROCEED
```

> **Note (manual mode):** the boundary/ref helpers (`side_effect()`,
> `record_external_operation()`) only take effect inside `@ledger` /
> `@ledger_sync` tool bodies. In manual claim mode they are ignored — durability
> comes from the claim itself: if the process dies after claiming, the entry
> expires and a redelivery hard-blocks instead of re-running.

Runnable examples (fakes only, no provider credentials):
[Stripe](examples/webhooks/stripe.md) (`event.id`) ·
[GitHub](examples/webhooks/github.md) (`X-GitHub-Delivery`) ·
[Twilio](examples/webhooks/twilio.md) (message/event SID).

**Failure-case pack (AF-002 gates):** five in-process repros for
`RETURN` / `POLL` / `HARD_BLOCK` (+ `REPAIR` / reconcile) — no Redis required.
See [examples/failure_cases/](examples/failure_cases/)
(`python examples/failure_cases/run_all.py` from `sdk/`).

## What `@ledger` / `ledger_sync` do

- Record every tool invocation in a durable `ActionLedger`
- Deduplicate retries and redispatches via a rich **transition key** (scope + tool + args + `side_effect_class` + policy), not only `tool_call_id`
- Resolve redispatches through **gates** (see [Resolution gates](#resolution-gates)) instead of re-running blindly
- Persist failed attempts with **terminal outcomes** (`FAILED_BEFORE_EFFECT`, `FAILED_AFTER_EFFECT`, `UNKNOWN`, `EXPIRED`, etc.) for audit and reconciliation

**Failure & threat model.** What this core can and cannot protect you from is
documented explicitly in
[`docs/FAILURE_AND_THREAT_MODEL.md`](docs/FAILURE_AND_THREAT_MODEL.md): the threat
actors (buggy redispatches, two workers, crash mid-effect, storage outage,
stalled worker, operator with backend access, provider indexing lag), the
guarantees the transition/ledger core actually provides, the guarantees it
deliberately does not (release authorization, runaway loops, trusting the
reconciler, in-memory ledgers across processes), and a **guarantee → test map**
so no documented promise is left without a test. It is honest about residual
risk — read it before relying on the runtime to stop a double payment.

### Transition identity and the `request_id` caveat

The transition key is a compound of runtime scope + tool name + canonicalised
args + `side_effect_class` + policy version — not `request_id` alone (or
`tool_call_id` alone):

| Inputs | → | Transition key |
|--------|---|----------------|
| Same `request_id` + same args | → | Same key — deduplicated, replayed, or polled as usual |
| Same `request_id` + **changed** args | → | **Different** key — the tool may execute again |

This is **intentional**: `request_id` records *which dispatch ticket* the
framework sent; it does not mean *what the tool is supposed to do*. Two
dispatches with the same ticket but different instructions are different
operations, not a conflict.

Some systems reject "same ticket, different meaning" as an identity conflict
(see our design partner Mengchheang Long's continuity harness). Mycelium does
**not** — not yet. An opt-in identity-conflict rejection mode (same
`request_id`, different args → reject) has been discussed but is **not
shipped**. If this gap matters for your deployment, open a GitHub issue.

The current contract is pinned in
`tests/test_mengchheang_public_repro.py::test_semantic_identity`:

```python
kwargs_a = {"amount": 10, "request_id": "intent-1"}
kwargs_b = {"amount": 11, "request_id": "intent-1"}
key_a = derive_transition_key_for_call("charge", (), kwargs_a, _BINDING)
key_b = derive_transition_key_for_call("charge", (), kwargs_b, _BINDING)
assert key_a != key_b       # changed args → different key
assert executions == [10, 11]  # both calls execute
```

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

Teachable in-process repros for the partner-facing gates:
[examples/failure_cases/](examples/failure_cases/) (`run_all.py`).

**Public transition-sufficiency language:** #7417-style discussions often use four words — `ALLOW` / `REPAIR` / `SOFT_BLOCK` / `HARD_BLOCK` (sometimes `BLOCK`). Mycelium implements that set and adds finer internals:

| Public | Mycelium | Notes |
|--------|----------|-------|
| `ALLOW` | `ALLOW` | run / safe retry |
| `REPAIR` | `REPAIR` | heal durable context; owner auto-renew / `renew_lease()` for a live lease |
| `SOFT_BLOCK` | `SOFT_BLOCK` | read-only defer / safe retry |
| `HARD_BLOCK` / `BLOCK` | `HARD_BLOCK` | stop; reconcile if ref present |
| *(must not run again)* | `RETURN` / `POLL` | already done, or wait on a held lease |
| *(read reclaim)* | `RECLAIM` | take over an expired read lease and run |

Public `BLOCK` ≈ Mycelium `HARD_BLOCK`. `RETURN` and `POLL` are also “do not execute again” under the richer internal taxonomy — use the four public words with platforms; use the full table when implementing or debugging.

**Lease validity (v1.10.0) / auto-renew (v1.14.0):** `lease_until` is resolution metadata — **not** part of `transition_key` (so renewals do not fork identity). Before reclaim/retry, resolution classifies the window via `LeaseValidity` (`HELD` → poll, `EXPIRED` → reclaim or hard-block by class, `UNBOUNDED` → no TTL). While a `@ledger` / `@ledger_sync` tool body runs, Mycelium **auto-extends** the lease (default every `lease_ttl / 3`). Set `lease_renew_interval: 0` to disable; call `renew_lease()` for an extra manual bump or when claiming outside the decorator.

**Cloud-style proof (v1.13.4):** `mycelium demo --redis` (or `prove_two_worker_redis_redispatch()`) runs **two OS processes** against a **real Redis** ledger. Worker A claims and runs; worker B redispatches the same `request_id` while A is `IN_FLIGHT`. B polls and returns A's result — the side effect runs once. Set `MYCELIUM_TEST_REDIS_URL` or use `redis://127.0.0.1:6379/15`. This is the partner-facing #7417 proof beyond an in-process double call.

**`REPAIR` (v1.13.0):** when the durable record is incomplete but healable (missing `idempotency_key`, invalid/missing `side_effect_boundary` or `terminal_outcome`, or status/terminal drift), claim loops call `repair_transition()` then re-resolve. A held in-flight lease is still `POLL` for peers; the owner keeps it held via auto-renew (or `renew_lease()`), not a second execute.

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

#### Gmail sent-log reconciler (`GmailReconciler`)

Email send tools often fail after the provider accepts the message but before the 250 OK reaches the agent. The ambiguous transition hard-blocks. A `GmailReconciler` resolves them automatically by checking the Gmail sent-log:

```python
from mycelium import ledger_sync, GmailReconciler, ReconcileResult

reconciler = GmailReconciler(service=gmail_client)  # duck-typed Gmail API

@ledger_sync(transition_binding=binding, reconciler=reconciler)
def send_email(to, subject, body):
    message_id = str(uuid4())  # RFC 2822 Message-ID generated before transport
    with side_effect():
        record_external_operation(message_id)
        mime_msg = build_mime(to, subject, body, message_id)
        smtp.sendmail(from_addr, to, mime_msg.as_string())
    return {"message_id": message_id}
```

The reconciler queries `users.messages.list(q='in:sent rfc822msgid:<Message-ID>')`:

| Matches | Result | Reasoning |
|---------|--------|-----------|
| 1 | `COMPLETED` | message landed; return the Gmail message object |
| 0 | `UNKNOWN` | indexing lag — never authorizes blind retry (`NOT_EXECUTED`) |
| 2+ | `UNKNOWN` | duplicate may already have occurred |
| missing ref | `UNKNOWN` | no query made |

Like all reconcilers, `GmailReconciler` is strict about indexing lag: zero matches means "not yet visible," not "never sent." The transition stays hard-blocked so an operator releases it when the provider confirms.

#### Field mapping for external verifiers

When wiring an independent verifier, keep the three identifiers separate:

| Identifier | What it is | Indexed by |
|------------|------------|------------|
| `request_id` / transition key | Mycelium's dispatch / ledger identity for the call | the Mycelium ledger |
| `external_operation_ref` | the handle recorded on the entry for read-only reconcile — "did this land?" | the `Reconciler` lookup |
| provider / third-party id (Stripe `pi_...`, Gmail Message-ID, ...) | the operation handle the external verifier indexes | the provider / verifier |

Terminal state is verifier-useful when the `Reconciler` queries an **independent** source: the ref is a handle, not proof by itself — proof is the read-only reconciler query, not the fact that a ref was recorded. Record the ref **before** the side effect (ideally the idempotency key you send, or a pre-generated Message-ID) so a crash between claim and complete can still be reconciled.

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

#### Payment-class identity (server-authoritative)

Never mint payment-class transition keys or provider keys from **raw client or
LLM args alone**. A caller that can tweak any arg re-mints a different key —
and can dodge an in-flight lease to start a second side effect. Derive identity
from **server-authoritative values** the caller cannot casually change: tenant,
mandate / intent hash, amount, recipient, network, and similar fields your
service controls.

Changing a *real* payment field (actual amount, recipient, mandate) → a new
transition is correct — it is a different operation. Tweaking fluff to escape
the key is what this rule blocks. Mycelium's compound transition key (scope +
tool + args + class + policy) does not, on its own, distinguish the two.

Recommended deterministic provider-key pattern:

```text
provider_key = HMAC-SHA256(server_secret, action_id)
```

Same `action_id` on retry mints the same provider key; the secret never leaves
your server. Pass the key through `provider_idempotency_key_param` so Mycelium
enforces same-key retry. Mycelium enforces the *same key on retry* when
configured; your application must mint **stable, server-side** keys. Keep no
wall-clock in the identity — retries must reproduce the same key.

### Operator runbook: your agent hard-blocked

When a side-effecting transition ends ambiguous (`BLOCKED` / `UNKNOWN` / `FAILED_AFTER_EFFECT`, or `EXPIRED` past the side-effect boundary) and no `Reconciler` can settle it, every redispatch raises `LedgerHardBlockError` forever. The release workflow is the recovery path: an operator verifies against the external provider what *actually* happened, records that verification, and the next agent redispatch consumes it. **Release is a recorded human verification, not an unblock** — and the CLI never executes tools itself.

**1. Triage what's stuck:**

```bash
mycelium transitions list --stuck --config mycelium.yaml
# or without the app's config, straight at the backend:
mycelium transitions list --stuck --sqlite ./mycelium-ledger.db
mycelium transitions list --stuck --redis-url redis://localhost:6379/0
```

Each row shows the request id, tool, resolved outcome, age, and a next-action hint. `--json` gives machine-readable output; `--tool NAME` filters.

**2. Inspect and verify with the provider:**

```bash
mycelium transitions show <request_id> --config mycelium.yaml
```

This prints everything needed for the provider lookup: tool + args, resolved outcome, `side_effect_boundary`, `lease_until`/`owner`, `error`, and crucially `external_operation_ref` (e.g. the Stripe `pi_...`) and `provider_idempotency_key`. Check the provider: did this operation actually complete?

**3. Record the verification:**

```bash
# Effect HAPPENED at the provider → record the result; redispatch returns it.
mycelium transitions release <request_id> --verified completed \
  --result-json '{"charged": true, "id": "pi_..."}' \
  --by ops@example.com --reason "pi_... succeeded in Stripe dashboard"

# Effect provably NEVER happened → the next redispatch re-executes exactly once.
mycelium transitions release <request_id> --verified not-executed \
  --by ops@example.com --reason "no charge for pi_... in Stripe; worker OOM-killed"
```

| `--verified` | Meaning | Next redispatch |
|--------------|---------|-----------------|
| `completed` | the effect happened; you supply the result | returns the recorded result — the tool body does **not** run again |
| `not-executed` | the effect provably never ran | consumes the release and runs the tool **exactly once** |

Release is **one-shot** (a recorded verification is never overwritten — a second release fails) and **fail-closed**: unknown request ids, already-`COMPLETED` transitions, and `IN_FLIGHT` transitions with a still-held lease (a worker may be alive) are all refused. Entries are never deleted — the resolution (`operator_resolution`, `resolved_by`, `resolution_reason`, `resolved_at`, `released_from_outcome`) is stamped onto the durable record, so `provider_idempotency_key` enforcement and audit history survive. When an `AuditReceiptEmitter` is configured on the ledger, releases also emit signed receipts.

Storage resolution: `--config` reads each tool's `ledger:` section (deduplicated); `--file PATH` / `--sqlite PATH` / `--redis-url` / `--postgres-dsn` (env: `MYCELIUM_LEDGER_FILE` / `MYCELIUM_SQLITE_PATH` / `MYCELIUM_REDIS_URL` / `MYCELIUM_POSTGRES_DSN`) point the CLI at a backend directly for operator machines without the app's config. `storage: memory` can't be reached from the CLI — it lives inside the agent process; use the Python API there.

The same workflow exists in Python (e.g. from a runbook script or an admin console):

```python
ledger = ActionLedger(storage=RedisLedgerStorage("redis://localhost:6379/0"))
for entry in ledger.list_transitions(stuck=True):
    print(entry.request_id, entry.tool, entry.resolved_terminal_outcome())
ledger.release(request_id, verified="not_executed",
               by="ops@example.com", reason="provider shows no charge")
```

> **Warning: backend access = release authority.** Anyone who can write to the ledger backend can release transitions — `--by` is an audit stamp, not authentication. Protect Redis/Postgres/file access like you protect production credentials, and prefer signed audit receipts (`audit_receipt:`) so releases are tamper-evident.

**4. (When `reclaim_requires_death_signal: true`) Assert worker death:**

When the death-signal gate is on, EXPIRED entries cannot be reclaimed or released until the operator asserts the worker is dead. This prevents reclaiming a transition from a worker that is merely paused (GC, storage partition, failing auto-renew).

```bash
# Assert the worker is dead so reclaim/release can proceed:
mycelium transitions mark-dead <request_id> \
  --by ops@example.com --reason "worker pod restarted, confirmed no heartbeat"
```

| Field | Description |
|-------|-------------|
| `last_heartbeat_at` | Auto-set on claim/renew; shows when the worker last checked in |
| `worker_dead_asserted_by` | Operator who asserted death (audit stamp) |
| `worker_dead_asserted_at` | Timestamp of the death assertion |

The `mark-dead` command refuses if the entry has a recent heartbeat within the grace window (`presumed_dead_after`) — the worker may still be alive. Add `--override-heartbeat` to bypass this check when the operator has direct evidence of death (e.g. they killed the pod). After asserting death, `release` proceeds normally. `show` includes heartbeat/death fields; `list --stuck` hints at `mark-dead` when needed.

> **Note:** the alive-worker release protection and claim-path gating only apply when `reclaim_requires_death_signal: true`. When off (the default), `release` and `claim` proceed exactly as they did in v1.15 — the heartbeat fields are tracked but not enforced. We recommend enabling the gate in production.

Python API:

```python
entry = ledger.mark_worker_dead_for(request_id,
    by="ops@example.com", reason="confirmed dead")
# now release can proceed
ledger.release(request_id, verified="not_executed",
               by="ops@example.com", reason="worker died before effect")
```

### Loop guard (AF-003): identical actions across new `tool_call_id`s

The action ledger deduplicates **retries of the same dispatch**. If the LLM emits a *new* `tool_call_id` each turn with the same tool + args, that is a new transition — the ledger allows it. Optional `loop_guard:` detects that thrash:

1. Soft — `ToolBoundaryError` (`violation=loop_detected`) with an `llm_message`; body does not run  
2. Hard — `LedgerHardBlockError`; **entire run** frozen until an operator releases it  

```yaml
loop_guard:
  storage: file
  path: ./mycelium-loop.json
  escalate_after_soft: 1
  consecutive_soft:
    read: 5
    idempotent_mutate: 3
    keyed_mutate: 2
    non_idempotent_mutate: 2
    irreversible: 2
```

Wrapper order: `@loop_guard` → `@ledger` → `@bounded` → `@protect` → `func`.

```bash
mycelium loops status --stuck --config mycelium.yaml
# or: mycelium loops status --file ./mycelium-loop.json

mycelium loops release <run_id> --verified clear|allow-once|abort-run \
  --by ops@example.com --reason "..."
```

| `--verified` | Meaning |
|---|---|
| `clear` | Wipe streak / soft flags; counting restarts at 0 |
| `allow-once` | Permit exactly one matching action hash, then re-arm |
| `abort-run` | Keep the run frozen |

Demo: `python examples/loop_guard_db_search.py` (from `sdk/`).

### Completion contract (AF-007): refuse terminal while required subtasks pending

AF-007 is when the agent presents work as **done** while a host-declared
checklist is still open. This is **not** “did we meet the user’s real goal?”
(that is AF-005 / judgment). Mycelium only gates against an **explicit**
contract.

| Kind | Still `pending` at terminal | Result |
|------|-----------------------------|--------|
| **required** | yes | **refuse** — `CompletionRefusedError` |
| **optional** | yes | **warn and allow** |

Resolved marks: `success` | `failed` | `abandoned` (abandoned needs a reason).
Scope: `run_id` (fallback `thread_id`); missing scope → warn and skip.

```yaml
completion:
  storage: file
  path: ./mycelium-completion.json
  required:
    - id: send_email
    - id: write_pr
  optional:
    - id: post_slack
```

```python
from mycelium import CompletionContract, wrap_final_message, execution_scope
from mycelium.transition import TransitionScope

contract = CompletionContract(required=["send_email"], optional=["slack"])
finalize = wrap_final_message(contract, lambda text: text)

with execution_scope(TransitionScope(thread_id="t", run_id="r1", node="end")):
    contract.mark("send_email", "success")
    # optional still pending → allow_with_warnings
    contract.complete_run()
    finalize("Done.")
```

Entry points (same gate): `complete_run()`, LangGraph
`completion_gate_end(contract, config=...)` before END, or
`wrap_final_message`. Wire at least one or the guard never fires.

```bash
mycelium completion status <run_id> --config mycelium.yaml
mycelium completion mark <run_id> send_email --status success
mycelium completion mark <run_id> post_slack --status abandoned \
  --reason "channel muted"
```

Demo: `python examples/completion_contract_checklist.py` (from `sdk/`).

### State authority: refuse decisions from superseded checkpoints

**Claim ≠ state authority.** The ActionLedger answers “has this logical
transition already been claimed/executed?” It does **not** answer “was this
call derived from state that is still current?”

Classic same-`tool_call_id` redispatch is covered by the ledger. The gap
without this gate: redispatch from a stale checkpoint S₀ that mints a *new*
`tool_call_id` (or changed args) → no prior claim → ledger PROCEEDs even
though the decision is outdated.

Optional `state_authority:` closes that gap **before** claim:

1. Host freezes `state_ref` (checkpoint id / state version / content hash) when
   the decision is made and passes it on each tool call (optional
   `decision_id` for audit).
2. Host supplies `get_canonical_state_ref(...)` — current canonical identity.
3. On mismatch (or missing ref when `require_state_ref: true`) → soft
   (`ToolBoundaryError`, `violation=state_superseded` / `state_ref_missing`)
   or hard (`LedgerHardBlockError`). Body does not run; no ledger claim.

```yaml
state_authority:
  canonical_callable: my_pkg.state:get_canonical_state_ref
  require_state_ref: true
  on_mismatch: hard   # soft | hard
  on_missing: hard
```

```python
from mycelium import StateAuthority, state_authority_sync, ledger_sync

def get_canonical_state_ref(*, tool, thread_id, run_id, kwargs):
    return current_checkpoint_id(thread_id)  # host-owned

authority = StateAuthority(
    get_canonical_state_ref,
    require_state_ref=True,
    on_mismatch="hard",
)

@state_authority_sync(authority)
@ledger_sync(storage=..., transition_binding=...)
def refund(amount: float, *, tool_call_id: str, state_ref: str) -> dict:
    ...
```

Wrapper order: `@state_authority` → `@loop_guard` → `@ledger` → `@bounded` →
`@protect` → `func`.

`decision_id` / `state_ref` are bookkeeping kwargs (excluded from the args
fingerprint) and are stored on `LedgerEntry` at claim for audit. Enforcement
stays in `StateAuthority`, not inside claim resolution.

Storage backends:

| Backend | Use case | YAML `storage` |
|---------|----------|----------------|
| `memory` | Single process, tests | `memory` (default) |
| `file` | Local dev, single host (`fcntl` lock) | `file` + `path` |
| `sqlite` | Zero-ops single-node durable (stdlib) | `sqlite` + `path` (+ optional `table`) |
| `redis` | Multi-worker, in-flight TTL | `redis` + `url` or `url_env` |
| `postgres` | Audit/compliance, durable SQL | `postgres` + `dsn` or `dsn_env` |

```python
from mycelium import ActionLedger, FileLedgerStorage, InMemoryLedgerStorage
from mycelium import RedisLedgerStorage, PostgresLedgerStorage, SqliteLedgerStorage

ledger = ActionLedger(storage=InMemoryLedgerStorage())
ledger = ActionLedger(storage=FileLedgerStorage("./mycelium-ledger.json"))
ledger = ActionLedger(storage=SqliteLedgerStorage("./mycelium-ledger.db"))
ledger = ActionLedger(storage=RedisLedgerStorage("redis://localhost:6379/0"))
ledger = ActionLedger(storage=PostgresLedgerStorage("postgresql://localhost/mycelium"))
```

```yaml
action_ledger:
  storage: sqlite
  path: ./mycelium-ledger.db
  # table: mycelium_action_ledger   # optional
```

CLI triage: `mycelium transitions list --stuck --sqlite ./mycelium-ledger.db`
(or `MYCELIUM_SQLITE_PATH`). Prefer Redis/Postgres for multi-worker / cross-node
retry; SQLite is the simple durable on-ramp (no extra deps).

Optional extras: `pip install 'mycelium-runtime[redis]'` or `pip install 'mycelium-runtime[postgres]'`.

### What happens when storage is down

Mycelium follows a **fail-closed** contract when the durable storage backend fails:

| Scenario | Behavior | Entry state |
|----------|----------|-------------|
| Storage fails during `claim()` | `LedgerStorageUnavailableError` raised; tool **never runs** | no entry |
| Storage fails during `complete()` / failure recording | Error propagates; entry stays `IN_FLIGHT` → lease expires → `EXPIRED` → hard-block/reconcile | `IN_FLIGHT` |
| Storage fails during `_record_failure` (tool already raised) | Original tool exception re-raised (storage error logged, **not** masked) | unchanged |

The original backend exception is preserved as `__cause__` on `LedgerStorageUnavailableError` for debugging. Storage errors never masquerade as tool errors and never allow silent data loss.

### Unclassified tools

Tools without a `transition_binding` (unclassified) have unknown side-effect semantics. The `unclassified_policy` controls how retries of failed entries are handled:

| Policy | Default | Behavior |
|--------|---------|----------|
| `warn` | yes | One-time `UserWarning` per tool when a failed entry is reclaimed; legacy behavior (re-execute) |
| `strict` | no | Routes through `claim_side_effecting` with a conservative binding (`non_idempotent_mutate`); failed retries **hard-block** instead of re-executing |

```python
# Decorator
@ledger_sync(unclassified_policy="strict")
def my_tool(...): ...

# YAML
action_ledger:
  storage: redis
  url_env: MYCELIUM_REDIS_URL
  unclassified_policy: strict
```

When `transition:` is configured and a side-effecting tool uses memory storage, a one-time warning is emitted at YAML load time — the duplicate-side-effect guard only holds within the process.

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
  # lease_renew_interval: 1200   # default = lease_ttl/3; 0 disables auto-renew
  # reclaim_requires_death_signal: false   # default; true = require mark-dead before reclaim
  # presumed_dead_after: 7200             # default = 2 × lease_ttl; grace window for heartbeat

action_ledger:
  storage: file
  path: ./mycelium-ledger.json
  unclassified_policy: strict   # warn (default) or strict
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

loop_guard:
  storage: file
  path: ./mycelium-loop.json

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

## Atomicity contract (v1.18+)

**Problem:** Two workers claim the same transition. Worker A completes. Worker B's stale `IN_FLIGHT` entry resolves later and silently overwrites A's `COMPLETED` result with a `FAILED_*` outcome. The operation's terminal state is lost.

**Solution:** Every terminal-outcome write goes through a CAS (`try_transition`) that checks the entry's current `terminal_outcome` and `owner` against expected values. Already-resolved entries refuse overwrites.

### Transition matrix (rejected transitions)

| Current `terminal_outcome` | `complete()` | `fail()` | `mark_blocked()` | `mark_unknown()` |
|---|---|---|---|---|
| `IN_FLIGHT` | ✅ allowed | ✅ allowed | ✅ allowed | ✅ allowed |
| `COMPLETED` | ❌ | ❌ | ❌ | ❌ |
| `BLOCKED` | ❌ | ❌ | ❌ | ❌ |
| `UNKNOWN` | ❌ | ❌ | ❌ | ❌ |
| `FAILED_BEFORE_EFFECT` | ❌ | ❌ | ❌ | ❌ |
| `FAILED_AFTER_EFFECT` | ❌ | ❌ | ❌ | ❌ |

Resolution paths (`release()` / reconcile) can complete from `BLOCKED`, `UNKNOWN`, or `FAILED_AFTER_EFFECT` — they pass a broader `_expected_from` set.

### Owner fencing

The `@ledger` / `@ledger_sync` wrapper captures the current worker's identity (`_ledger_owner()`) and passes it to `complete()` and `_record_failure`. If a different worker tries to resolve the same entry, the CAS rejects it with `LedgerOutcomeAlreadySetError`. In `_record_failure`, the CAS error is caught and the original tool exception is re-raised — never masked.

### Backend implementation

| Backend | CAS mechanism |
|---------|--------------|
| Memory | `InMemoryLedgerStorage` delegates to `set()` when CAS matches |
| File | Within `LockedJsonDictFile.read_modify_write` |
| Redis | `pipe.watch()` on the key; `WatchError` retry loop on conflict |
| Postgres | `UPDATE ... WHERE payload->>'terminal_outcome' = ANY(...) RETURNING` |

### NOT_EXECUTED reset CAS (v1.18+)

The `NOT_EXECUTED` reset path (reconcile → fresh `IN_FLIGHT` claim) uses a CAS on
`_RECONCILE_NOT_EXECUTED_OUTCOMES`. When two reconcilers both return
`NOT_EXECUTED`, the CAS loser reads the winner's entry and returns it to the
claim loop, which polls until the winner completes rather than hard-blocking.
The same stale-snapshot guard applies in `_raise_hard_block`: a re-read that
finds `IN_FLIGHT` with a live lease returns to the claim loop instead of
raising, and `mark_blocked` is never called on an entry whose lease is
currently held.

## Outcome telemetry & DTTR (v1.20+)

**Problem:** You run a fleet of agents and want a single number that proves
the duplicate-side-effect guard is holding in production — and that regresses
loudly the day it doesn't.

**Solution:** opt-in `OutcomeEmitter` resolution telemetry plus a pinned
metric, the **Duplicate Tool Transition Rate (DTTR)**, computed after the fact
from flat append-only rows. Off by default; memory or file storage only in v1
(no analytics SaaS dependency).

Enable it in YAML and every ledgered tool starts emitting:

```yaml
outcome_emit:
  storage: file                 # memory | file
  path: ./mycelium-outcomes.jsonl
  long_running_after: 3600      # seconds (default: lease_ttl)
```

Or pass an emitter directly:

```python
from mycelium import OutcomeEmitter, ledger_sync

emitter = OutcomeEmitter(agent_id="acme", storage=FileOutcomeStorage("outcomes.jsonl"))

@ledger_sync(storage=..., transition_binding=..., outcome_emitter=emitter)
def charge(amount):
    ...
```

Rows are one JSON object per line, emitted only on resolution events — a
dispatch resolving to a gate (`ALLOW` / `RETURN` / `HARD_BLOCK` /
`SOFT_BLOCK`), the tool body starting / completing / failing, and operator
releases. Poll ticks never emit. Emission is fault-tolerant: a storage
failure is logged and swallowed, so telemetry can never break the tool path.

Compute the metric with the CLI or the library:

```console
$ mycelium outcomes dttr --file ./mycelium-outcomes.jsonl
DTTR: 0.0000  (target: 0.0)
silent duplicates: 0  long-running or redispatched: 3  transitions: 42
```

```python
from mycelium import FileOutcomeStorage, compute_dttr_from_storage

report = compute_dttr_from_storage(FileOutcomeStorage("outcomes.jsonl"), long_running_after=3600)
```

### DTTR definition

- A **transition** is every row sharing a `request_id` (the transition key).
- A **silent duplicate** is a tool-body execution for a transition that had
  already executed, without being authorized by a consumed `NOT_EXECUTED`
  verdict (reconciler `NOT_EXECUTED` or an operator release verified
  `not_executed`). The first execution is always authorized; each consumed
  `NOT_EXECUTED` authorizes exactly one more, so the guarantee to measure is
  `executions <= 1 + not_executed_verdicts`.
- A transition is **long-running or redispatched** when it saw ≥2 resolution
  events (framework redispatches) OR its wall-clock span exceeds
  `long_running_after`.
- `DTTR = silent_duplicates / max(long_running_or_redispatched, 1)`. The
  target is **0.0**. Without Mycelium, two workers racing the same effect
  produce a silent duplicate (DTTR > 0); with the guard, duplicate body runs
  only happen through an authorized `NOT_EXECUTED` path.

## For contributors (repo layout)

Clone the GitHub repo to run proofs and tests. PyPI installs only the `mycelium` package.

```bash
git clone https://github.com/mycelium-labs/mycelium.git
cd mycelium/sdk && pip install -e ".[dev]"
pytest tests/ -v
```
