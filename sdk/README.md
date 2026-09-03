# Mycelium runtime

[![PyPI version](https://img.shields.io/pypi/v/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)
[![Python](https://img.shields.io/pypi/pyversions/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)

**The reliability layer for AI agents** — installable as `mycelium-runtime`
(current version on [PyPI](https://pypi.org/project/mycelium-runtime/)).

The public story is the [failure-mode catalog](docs/FAILURE_MODE_CATALOG.md) (**AF-001…AF-012**): each ID is a real runtime failure class; each shipped surface is a deterministic guard. The taxonomy is the product promise; envelope fields and gates are how AF-002 is implemented underneath.

**Releases:** batch; calm over velocity — [release policy & pre-release checklist](docs/RELEASE.md).

**AF-002 flagship:** any tool, any provider — prove run-or-not and enforce at-most-once (ledger · lease · `Reconciler` · operator release). Provider adapters (Gmail sent-log, Stripe-shaped examples) are demos of that contract, not the headline.

Also shipping: destination-aware effect identity + unified `EffectState`; fenced CAS
and atomic decision predicates/records; and `ToolCapability`-aware recovery. The wider
surface includes AF-003/004/006/007/008; the AF-010–AF-012 side-effect guardrail batch
(secret and destination checks, destructive grants, authority expiry, and use-time
currency); SQLite + Redis/Postgres; DTTR; worker-death detection; and lease auto-renew.

## One painful bug → a few lines of config

Prefer agent-assisted setup? The PyPI package includes the official
[`mycelium-setup`](https://github.com/mycelium-labs/mycelium/tree/main/.agents/skills/mycelium-setup)
skill. Run `mycelium skills install` after installing the package, then ask your
coding agent to set up Mycelium. The agent can inspect the application,
fill/merge YAML, wire the real tool boundary, add tests, and run Doctor/Verify.
It remains fail-closed for secrets, business identity, and provider authority
that cannot be safely inferred.

**LangGraph Cloud redispatches a long tool call while the first is still running.** Both complete. You pay twice. Side effects run twice. [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417) — catalog class **AF-002**.

Mycelium’s answer is a provider-reconciled, operator-releaseable, auditable **execution ledger** (the transition envelope under AF-002): **any tool, any provider** — prove run-or-not and enforce **at-most-once**. Claim before the side effect, hold a **lease** while work is in flight, record **terminal state**, and **hard-block** (or reconcile with the provider) when a mutating redispatch would be unsafe. Same key while in-flight → poll; completed → return stored; ambiguous mutate → stop. Not “idempotency key + cached result” alone.

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

Mycelium sits between your agent loop and your tools (after the LLM returns `tool_calls`). Promise first (catalog), mechanism second:

| | Catalog class | What Mycelium does |
|---|---------------|-------------------|
| **Core** | **AF-002 Observability black hole** | **Flagship:** any tool, any provider — prove run-or-not, at-most-once. Ledger · lease · gates · `Reconciler` · operator release · receipts (Gmail/Stripe adapters = demos) |
| **Opt-in** | **AF-003 Infinite action loops** | `loop_guard:` — action-hash streak across *new* `tool_call_id`s; soft then hard; operator `mycelium loops release` |
| **Opt-in** | **Budget / runaway spend (unnumbered)** | `budget:` — `max_duration` / `max_steps` / `max_tokens` / `max_usd`; tools auto-wrapped; **LLM turns auto-wired** on LangGraph/LangChain; `missing_usage_policy`; operator `mycelium budget release` |
| **Opt-in** | **AF-010 Secret-in-args** | `secret_args:` — block raw credentials before claim; pass `secret://` references; shared sanitizer on evidence. Fail-closed is primary; redaction is defense-in-depth |
| **Opt-in** | **Entity / destination guard (unnumbered)** | `entity_guard:` — a write may carry sensitive data only into a host-authorized destination; unknown destination means no execution |
| **Opt-in** | **AF-011 Destructive confirm** | `destructive_confirm:` — tool permission is not object authorization; a host-issued grant is required for the exact operation and canonical object |
| **Opt-in** | **Authority-window expiry** | `authority_window:` — re-validate time-bounded authority at use (after lease/backoff, before `mark_maybe_crossed`) |
| **Opt-in** | **AF-012 Use-time currency** | `use_time_currency:` — revalidate decide-time facts at use; stale/changed/missing/unverifiable facts cannot authorize a side effect |
| **Opt-in** | **AF-004 Tool misuse** | `@bounded` input/output/scope checks; optional `ToolRegistry` allowlist — block before the tool runs |
| **Opt-in** | **AF-006 Context corruption** | TTL cache (`@protect` / `Session`); optional `MessageValidator` / `HistoryGuard` before the next LLM turn |
| **Opt-in** | **AF-007 Premature termination** | `completion:` — host checklist; unmarked **required** → refuse terminal; unmarked **optional** → warn and allow |
| **Opt-in** | **AF-008 Scope escalation** | `scope_guard:` — freeze run tool allowlist; re-check every step; mid-run / handoff widen → `ToolBoundaryError` |
| **Default-on** | **AF-002 Args drift** | `action_ledger.on_args_drift` — same call id + different args within a run (default `soft`; `hard` / `off` opt-in) |
| **Opt-in** | **Superseded state** | `state_authority:` — freeze `state_ref` at decide time; compare to host canonical ref before claim |

Envelope field stack (`side_effect_class` → spendability → boundary → …) is documented under [Transition envelope fields](#transition-envelope-fields) — implementation detail for AF-002, not the product headline.

`mycelium init` / `mycelium run` center on AF-002. Other catalog guards are available when you configure them.

Framework-agnostic. Raw message lists and plain Python functions (LangGraph, CrewAI, OpenAI tool loops, etc.).

### Framework integration support

Framework-agnostic means the core ledger can wrap plain Python callables. It
does not mean every framework provides the same automatic runtime identity or
terminal hooks. AF-002 depends on stable transition identity, so choose the
integration path explicitly:

| Runtime | Tool identity | Ledger integration | Additional integration |
|---|---|---|---|
| LangGraph `ToolNode` / `create_agent` | Automatic from injected `ToolRuntime` when `integrations.langgraph` is enabled | YAML, `@config.apply`, or ledger decorators | Automatic budget and completion adapters when configured |
| CrewAI | Automatic logical dispatch identity from crew/run/task/agent metadata when `integrations.crewai` is enabled; host-supplied `request_id` remains required for consequential production tools | YAML, `@config.apply`, or ledger decorators | Automatic completion terminal; `instrument_crewai_llm` provides budget accounting |
| Plain Python or another Python framework | Host-supplied identity is recommended | Decorators or [manual claim/complete](#manual-integration-claim--execute--complete) | Host calls completion, budget, and scope hooks explicitly |
| TypeScript or another non-Python runtime | No native identity adapter | No native SDK; place the guarded operation behind a Python service boundary or implement the transition protocol in the host | Host-owned |

For consequential operations, prefer a stable host-owned business identifier
such as `charge-order:ORD-123` over a random or model-generated dispatch ID.
Automatic LangGraph metadata is convenient for redispatch detection, but it
does not replace business identity when two different tool calls represent the
same real-world action. See
[Transition identity and host-owned `request_id`](#transition-identity-and-host-owned-request_id).

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
mycelium skills install    # offline → ./.agents/skills/mycelium-setup
pip install 'mycelium-runtime[langgraph]'  # optional automatic LangGraph IDs
mycelium init              # on-ramp: duplicate-tool fix → ./mycelium.yaml
mycelium init --detect     # inspect local dependencies/@tool functions and tailor a safe starter
mycelium init --full       # reference: every guard section (not the default)
mycelium init --minimal    # smaller multi-guard scaffold
mycelium config schema -o mycelium.schema.json  # JSON Schema / IDE completion
mycelium config docs       # reference generated from the typed model
mycelium config example    # model-validated example YAML
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

Field spec keys: `type` (`string`, `integer`, `number`, `boolean`, `array`
with `items`, `object` with optional `additional_properties`), `required`,
`pattern`, `min_length`, `max_length`. Unknown type names raise
`SchemaBuildError` at decorate time. You pass plain dicts; Mycelium validates
internally; no Pydantic imports in your code.

## What `@bounded` / `bounded_sync` do

- `@bounded` / `bounded_sync`: validate tool args against your field spec **before** the function runs
- `output_schema`: validate the return value **after** the function runs; bad results are not propagated
- `allowed_paths` / `entity_pattern`: user-defined scope gates (paths under
  allowlisted roots after canonical path and symlink resolution, entity ID
  format). Missing descendants are supported. Resolution errors fail closed.
- On failure, raises `ToolBoundaryError` with `llm_message` for the agent loop; does not retry by itself

`allowed_paths` validates the resolved path immediately before dispatch, but
it is not a filesystem sandbox. Another process that can replace path
components after validation can create a time-of-check/time-of-use race. Use
OS sandboxing or descriptor-relative operations inside the tool when the
filesystem is writable by an adversary.

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

Stop duplicate payments, emails, and API calls when the framework retries. Five
**effect-semantic** `side_effect_class` values describe retry safety, while
`capability` (`idempotent` / `queryable` / `blind`) describes whether recovery
can determine what happened. Reads poll in-flight duplicates; ambiguous blind
effects park instead of being re-executed.

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

CrewAI uses scoped lifecycle hooks, so tool functions do not need a new
parameter:

```yaml
integrations:
  crewai:
    enabled: true
    run_id_from: ticket_id
```

Install `mycelium-runtime[crewai]`, then pass the stable field through the
existing `Crew.kickoff(inputs=...)` mapping. Mycelium combines that run value
with CrewAI's crew, task, agent, tool, and canonical tool arguments before a
configured tool executes. If `run_id_from` is omitted, development mode derives
a deterministic run scope from the crew and full kickoff inputs.

CrewAI's public tool hooks do not expose the model provider's tool-call ID, so
the adapter deliberately uses this conservative logical identity. Repeating an
identical tool call in the same task maps to the same dispatch. If two identical
calls are genuinely different business actions, give them distinct stable
host-owned `request_id` values. Framework identity never replaces that
production business-identity requirement.

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

        ledger.record_decision(
            request_id,
            {"allowed": True, "verdicts": [], "denied_reasons": []},
            expected_owner=entry.owner,
            expected_fence=entry.fence,
        )
        # PROCEED: we hold IN_FLIGHT — run the side effect once, then settle.
        try:
            result = gateway.charge(amount, recipient)
        except Exception as exc:
            # The provider may have accepted before raising: park ambiguity.
            ledger.fail(
                request_id, exc, failed_after_effect=True,
                expected_fence=entry.fence,
            )
            raise
        ledger.complete(request_id, result, expected_fence=entry.fence)
        return result
```

| Step | Meaning |
|------|---------|
| `claim_side_effecting(...)` | May I run? Resolves gates (`RETURN` / `POLL` / `HARD_BLOCK` / …). Raises on hard-block. |
| `COMPLETED` → return `entry.result` | Partner-facing **SKIP** — already done. |
| `record_decision(...)` | Atomically records the final allow/deny result and advances `INTENDED → ATTEMPTING`. |
| Else run body + `complete(..., expected_fence=entry.fence)` | Partner-facing **PROCEED** then settle. |
| `fail(..., expected_fence=entry.fence)` | Settle a failure; use `failed_after_effect=True` when the effect may have happened. Use `False` only when failure is proven pre-effect. |

The decorators evaluate registered decision predicates at this single boundary
and record their verdicts automatically. A manual integration must record its
equivalent final decision before the external effect. Every mutation made on
behalf of a claim—including decision, boundary, heartbeat, provider-reference,
failure, and completion writes—must carry that claim's `entry.fence`; after a
takeover increments the stored fence, stale-worker writes are rejected.

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
    ledger.record_decision(
        request_id,
        {"allowed": True, "verdicts": [], "denied_reasons": []},
        expected_owner=entry.owner,
        expected_fence=entry.fence,
    )
    try:
        result = do_work()                  # the side effect, once
    except Exception as exc:
        # Handler effects may precede the exception: park ambiguity.
        ledger.fail(
            request_id, exc, failed_after_effect=True,
            expected_fence=entry.fence,
        )
        return 500
    ledger.complete(request_id, result, expected_fence=entry.fence)
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

### Effect-commit protocol

Ledger rows for tools with a `ToolTransitionBinding` enable the effect-commit
protocol: they store a deterministic, destination-aware `effect_id`,
`schema_version`, claim `fence`, atomic `decision`, and unified effect state.
Unclassified `claim()` rows keep the protocol disabled; because no binding
exists, their compatibility `effect_id` falls back to `request_id` rather than
the destination-aware derivation.

### Ledger schema migrations

Ledger schema 2 adds durable `effect_id`, `request_id_aliases`, and
`schema_version`. The top-level `schema_version` field is the ledger row's
versioned envelope; there is no separate wrapper. Older rows remain readable
without migration, but deployments that retain durable ledgers can rewrite them
explicitly:

```bash
# Read-only preview. Repeat the storage flag for the apply command.
mycelium migrate --plan --sqlite mycelium-ledger.db

# Stop workers and back up the ledger before applying.
mycelium migrate --apply --sqlite mycelium-ledger.db

# Verify that no older rows remain.
mycelium migrate --plan --sqlite mycelium-ledger.db
```

The v1→v2 rule sets a missing `effect_id` to the existing `request_id`, sets
`request_id_aliases` to include that canonical request id, and writes
`schema_version: 2`; it never invents an empty identity. Planning does not
rewrite ledger rows, application is idempotent, unsupported future versions
fail closed during both normal reads and migration planning, and active
`IN_FLIGHT` rows are refused unless `--allow-active` is given after workers are
confirmed stopped. The same `--file`, `--sqlite`, `--redis-url`,
`--postgres-dsn`, or `--config` storage selection used by operator commands is
supported.

`mycelium doctor` inspects durable ledger versions without creating tables or
rewriting rows. With connectivity enabled it reports PASS for current rows,
WARN when migration is available, and FAIL for malformed or unsupported future
versions. `--no-connectivity` reports this check as SKIP.

Rollback is restore-based: before `--apply`, snapshot/copy the file or SQLite
database, take a Redis snapshot, or use a Postgres backup/transactional snapshot.
If rollback is needed, stop workers, restore that backup, and run the older
Mycelium version. In-place downgrades are intentionally refused because an old
schema cannot represent every newer record safely.

`request_id_aliases` records every request id that resolved to the same
canonical effect row. It preserves redispatch history; it does not create a new
effect or grant permission.

## Public API namespaces

Existing imports from `mycelium` remain supported; this namespace change does
not require an application migration. New code should use the package root for
the recommended API, `mycelium.runtime` for stable low-level building blocks,
and `mycelium.integrations` for optional framework adapters. APIs incubating
without a full stability promise live under `mycelium.experimental`. Nothing
under `mycelium._internal` is public.

```python
from mycelium import ledger_sync, load_config
from mycelium.integrations import instrument_langgraph_tool
from mycelium.runtime import ActionLedger, TransitionScope
```

The reviewed contract is stored in `api-snapshot.json` and checked by the test
suite on every CI run. An intentional public API change must preserve existing
imports or include deprecation metadata, compatibility tests, and a regenerated
snapshot (`python scripts/update_api_snapshot.py`).

The package root still re-exports historical low-level APIs for compatibility.
Moving an import to `mycelium.runtime` is optional until a separately announced
deprecation says otherwise.

`EffectState` is the durable write-ahead intent:

| State | Meaning |
|---|---|
| `INTENDED` | Row exists; no allow decision has been recorded |
| `ATTEMPTING` | The atomic decision allowed; the provider boundary may be crossed |
| `COMMITTED` | The ledger records the effect as completed |
| `ABORTED` | Denied or failed before the effect |
| `UNKNOWN` | The effect may have happened; redispatch stays fail-closed until resolved |

Use `resolve_effect_state(entry)` or `entry.resolved_effect_state()`. It folds
legacy `terminal_outcome`, boundary, decision, and `effect_phase` rows into the
unified view without a storage migration. `terminal_outcome` remains available
for compatibility and detailed failure labels.

`derive_effect_id_for_call()` uses the same canonical SHA-256 preimage as the
derived transition key: execution scope, dispatch identity, tool,
canonicalized meaningful arguments, canonical destination, side-effect class,
agent, and policy. Canonically equivalent destinations produce the same id;
different destinations do not. With derived identity, `request_id` and
`effect_id` coincide. With an explicit business `request_id`, consequential
tools still derive `effect_id` first and treat it as the authoritative dedupe
identity. If another `request_id` resolves to the same `effect_id`, Mycelium
routes to the canonical row and records the supplied id in
`request_id_aliases` for audit instead of creating a second row.

Every successful claim increments `LedgerEntry.fence`. Every later mutation
for that claim—decision, boundary, heartbeat/lease, provider reference,
receipt, completion, failure, reconciliation, and operator resolution—uses a
storage CAS that requires the same fence. A worker resumed after takeover
cannot mutate the winner's row even if stale owner or lease metadata still
looks plausible.

Policy checks compose as pure `(DecisionIntent, DecisionSnapshot)` predicates.
`register_decision_predicate(name, predicate)` adds a host predicate at the
single enforcement point. The combined `Decision` and every predicate verdict
are sanitized and persisted atomically with the fenced
`INTENDED → ATTEMPTING` transition. A denial records `ABORTED`; the
`@ledger` / `@ledger_sync` and YAML/config wrapper paths do not invoke the body
unless the allowed decision write succeeded. A stale-fence worker cannot
record a decision. A manual integration must call `record_decision(...)` with
the claim fence and wait for success before invoking the body or provider;
Mycelium cannot stop a manual host from calling a provider outside its APIs.

**Failure-mode catalog.** Stable AF-001…AF-012 definitions (shipped vs roadmap)
live in [`docs/FAILURE_MODE_CATALOG.md`](docs/FAILURE_MODE_CATALOG.md).

**Formal state model.** Optional TLA+ notes for the core EffectState protocol
live in [`docs/spec/README.md`](docs/spec/README.md) and
[`docs/spec/effect_state.tla`](docs/spec/effect_state.tla).

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

### Transition identity and host-owned `request_id`

Pass an optional `request_id` when the host already has a stable business
operation identity. Mycelium preserves that string for audit and deterministic
redispatch routing, but consequential dedupe is anchored on the independently
derived `effect_id`, not on host string equality. `request_id` is never hashed
with args and is not forwarded into the wrapped tool.

**The host must derive it from a server-owned record**, never from the model:

```python
request_id = f"charge-order:{order_id}"
charge(amount=10, recipient=acct, request_id=request_id)
```

For `keyed_mutate` tools, declare `provider_idempotency_key_param`; by default
Mycelium injects `effect_id` as that provider key when your call omits it, and
persists/reuses the same value across retries. If you pass a key explicitly,
that explicit value wins and is enforced on retry.

| Inputs | → | What happens |
|--------|---|--------------|
| Same explicit `request_id` + same tool/scope/args | → | Same transition — return stored result or poll |
| Different explicit `request_id` + same derived `effect_id` | → | Canonical prior row wins; second id is recorded in `request_id_aliases` |
| Same explicit `request_id` + **different** tool, scope, or meaningful args | → | Fail-closed identity conflict (`ToolBoundaryError` / hard-block) |
| `request_id` omitted (development / `derived`) | → | Unchanged derived identity (`tool_call_id` + scope + args + policy) |
| `request_id` omitted (production / `require_explicit`) | → | `MissingRequestIdentityError` for consequential tools |

Mycelium does **not** infer “this is a retry” from arguments alone. If you
omit `request_id` under the development `derived` policy, a new
`tool_call_id` is a new transition. Production does not accept
`tool_call_id` as a business ID.

Optional YAML helper — derive identity from one trusted server-owned
argument. Explicit `request_id` still wins. Mycelium does not infer
identity from arbitrary fields:

```yaml
tools:
  charge_customer:
    side_effect_class: keyed_mutate
    request_id_from: order_id
    provider_idempotency_key_param: idempotency_key
```

That mints `charge_customer:order_id:ORD-123`. With
`provider_idempotency_key_param` declared, keyed-mutate retries now inherit and
reuse the stored provider key (auto-injected from `effect_id` when omitted), so
provider dedupe stays aligned even when callers do not pass the key manually.

When `request_id` is omitted, the derived transition key still encodes args
(same `tool_call_id` + different args → different key). **By default Mycelium
also refuses the second body** so a corrupted upstream redispatch cannot
double-execute.

**Args-drift / identity-conflict gate (AF-002):** default
`action_ledger.on_args_drift: soft`. Mycelium compares claim-time
`args_fingerprint` to prior entries for the same dispatch ticket. Explicit
`request_id` conflicts include a changed tool or scope and are always refused,
including when `on_args_drift` is `off`. Derived `tool_call_id` drift checks are
scoped to the same run (`run_id`, else `thread_id`) — other runs are isolated.
Soft → `ToolBoundaryError` (loop can retry with original args or a new ticket);
hard → `LedgerHardBlockError` (freeze for a human); `off` → escape hatch
restoring "new args = new transition" dual execution for derived identities
only. Same ticket + same args still idempotently returns.

```yaml
action_ledger:
  on_args_drift: soft   # soft (default) | hard | off
```

```python
@ledger_sync(storage=storage, transition_binding=binding)  # soft by default
def charge(amount: int) -> int: ...

@ledger_sync(storage=storage, transition_binding=binding, on_args_drift="hard")
def charge_strict(amount: int) -> int: ...
```

Default soft is pinned by `tests/test_args_drift.py::test_default_is_soft_not_off`.
Key split + the `off` escape hatch are covered by
`tests/test_mengchheang_public_repro.py::test_semantic_identity`:

```python
# Derived identity (no request_id): hash still splits on args.
kwargs_a = {"amount": 10, "tool_call_id": "tc-1"}
kwargs_b = {"amount": 11, "tool_call_id": "tc-1"}
key_a = derive_transition_key_for_call("charge", (), kwargs_a, _BINDING)
key_b = derive_transition_key_for_call("charge", (), kwargs_b, _BINDING)
assert key_a != key_b       # changed args → different derived key
# default soft: second body raises ToolBoundaryError (does not run)
# on_args_drift="off": both execute (escape hatch for derived keys only)

# Explicit request_id is the storage identity and stays fail-closed.
charge(amount=10, request_id="charge-order:ORD-123")
charge(amount=10, request_id="charge-order:ORD-123")  # stored result
# charge(amount=11, request_id="charge-order:ORD-123")  # identity conflict
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

Decorator claim paths already poll. For a custom async LangGraph node that
already knows the peer `request_id` and only needs to wait (no re-claim):

```python
entry = await ledger.wait_for_transition_async(request_id)
# entry is no longer IN_FLIGHT — usually COMPLETED with entry.result
```

`wait_for_transition` is the sync twin. Timeout raises `LedgerPollTimeoutError`
and does **not** mark `UNKNOWN` (claim loops own that policy).

**Thin handoff identity (audit causation):** after a supervisor/spawn claim,
wrap subagent tool calls in `handoff_scope(parent_request_id, handoff_id=…)`
so child ledger entries record `parent_request_id` / `handoff_id`. Audit glue
only — does not grant capabilities or change at-most-once claim keys.
`list_transitions(parent_request_id=…)` and
`mycelium transitions list --parent` / `show` expose the link.

```python
from mycelium import handoff_scope

# parent_id = the supervisor transition's request_id
with handoff_scope(parent_id, handoff_id="spawn-pay"):
    charge(amount=10, tool_call_id="child-1")  # stamped with parent link
```

Teachable in-process repros for the partner-facing gates:
[examples/failure_cases/](examples/failure_cases/) (`run_all.py`).

**Copy-paste adoption (LangGraph + Redis + receipts + crash):**
[examples/langgraph_redis_crash/](examples/langgraph_redis_crash/) — one runnable
script that redispatches a LangGraph tool against a real Redis ledger (body once
+ signed receipt), then kills a mid-flight worker and shows `HARD_BLOCK` on
redispatch. Capability already shipped; this is the drop-in recipe.

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

For a **copy-paste** LangGraph + Redis + receipts + crash recipe (YAML + `run.py`),
see [examples/langgraph_redis_crash/](examples/langgraph_redis_crash/).

**`REPAIR` (v1.13.0):** when the durable record is incomplete but healable (missing `idempotency_key`, invalid/missing `side_effect_boundary` or `terminal_outcome`, or status/terminal drift), claim loops call `repair_transition()` then re-resolve. A held in-flight lease is still `POLL` for peers; the owner keeps it held via auto-renew (or `renew_lease()`), not a second execute.

**Read-only** (`side_effect_class: read`): poll, reclaim, retry failed-before-effect, soft-block on ambiguous `UNKNOWN`/`BLOCKED`.

**Mutating** (payment, email, subagent, irreversible, …): return completed, poll in-flight, hard-block ambiguity. For **`EXPIRED + not_crossed`**, the gate is `HARD_BLOCK` until a reconciler proves the effect never ran — see [Stale lease + reconcile](#stale-lease--reconcile-exired--not_crossed).

### Transition envelope fields

Seven recovery axes decide whether an unresolved prior execution is merely
**wasteful** (safe to retry/poll) or **unsafe** (must not re-run). Priority
order:

| # | Field | Role |
|---|-------|------|
| 1 | `side_effect_class` | What kind of effect (`read`, `keyed_mutate`, `non_idempotent_mutate`, …) |
| 2 | `capability` | Whether recovery is intrinsically safe, can query the outcome, or is blind (`idempotent` / `queryable` / `blind`) |
| 3 | `spendability` | How many times the same intent may spend (`multi_use` / `single_use` / `non_replayable`) |
| 4 | `side_effect_boundary` | Whether the external call was crossed (`not_crossed` / `maybe_crossed` / `crossed`) |
| 5 | `terminal_outcome` | Where the prior attempt ended (`IN_FLIGHT`, `COMPLETED`, `UNKNOWN`, `EXPIRED`, …) |
| 6 | `external_operation_ref` | Provider handle for read-only reconcile (id or idempotency key) |
| 7 | `retry_permission` | Whether automatic retry is allowed (and same-key enforcement when opted in) |

**Invariant:** for a given tool class, the fields that class **requires** must already be **supported and recorded** on the transition before a redispatch is treated as a safe retry. Reads need a lighter set (class + terminal + lease). Payment / write / email / subagent need spendability, boundary, terminal outcome, and usually an external receipt/ref — without them, a second dispatch is an **unsupported second transition**, not a retry.

For a classified, bound transition, the effect-commit record additionally
carries `effect_id`, `EffectState` (resolved from the compatibility
`effect_phase` storage field), `schema_version`, atomic `decision`, `fence`,
`transition_key`, `idempotency_key`, `owner`, `lease_until`, and
`receipt_ref`.

### Side-effect classes

| Class | Typical use | Duplicate handling |
|-------|-------------|-------------------|
| `read` | search, fetch | poll / reclaim / retry; `SOFT_BLOCK` on `UNKNOWN` |
| `idempotent_mutate` | upsert / set status | retry if boundary not crossed |
| `keyed_mutate` | Stripe-style create/charge | retry only with same provider key |
| `non_idempotent_mutate` | send email, spawn subagent | hard-block on ambiguity |
| `irreversible` | wire / on-chain burn | hard-block → human |

Legacy aliases (`read_only`, `payment`, `subagent`, …) still parse. Set per tool in YAML with `side_effect_class`. Required when `transition:` is configured and the tool is ledgered.

### Tool capabilities

`capability` is orthogonal to `side_effect_class`: the class answers whether a
second call is safe, while capability answers whether an unfinished call's
outcome can be established.

| Capability | Recovery contract |
|------------|-------------------|
| `idempotent` | Repeating the operation is intrinsically safe. Derived for `read` and `idempotent_mutate`. |
| `queryable` | A reconciler or provider idempotency key can establish/deduplicate the outcome. Derived for supported classes when that mechanism is configured. |
| `blind` | The outcome cannot be probed; ambiguous entries never auto-redispatch and park for operator reconciliation. |

Configure it per tool with `capability:` or in code with
`ToolTransitionBinding.for_tool(..., capability=ToolCapability.QUERYABLE)`.
Omitting it derives the conservative value from the side-effect class and the
configured provider key/reconciler. An explicit declaration may tighten to
`blind`, but cannot claim a looser capability than the available mechanism
supports. Declaring `queryable` without a usable probe fails closed to blind
parking; `irreversible` always remains blind.

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

Because `maybe_crossed` is written durably *before* the call, a process crash mid-call leaves the entry ambiguous and a redispatch hard-blocks instead of double-spending. For finer control use `mark_maybe_crossed()` / `mark_crossed()` directly. Async tools use `async with side_effect_async()` so async use-time validators are awaited at the final boundary.

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

**This is the AF-002 flagship path:** any tool, any provider — you record a handle, Mycelium asks the provider whether the effect landed, and redispatch is at-most-once. Shipped provider classes are reference adapters; the contract is the story.

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

#### Demo adapter: Gmail sent-log (`GmailReconciler`)

Not the product story — a shipped **demo** of the general `Reconciler` contract (prove run-or-not; fail closed on indexing lag). Email send tools often fail after the provider accepts the message but before the 250 OK reaches the agent. The ambiguous transition hard-blocks; `GmailReconciler` asks the Gmail sent-log:

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

The reconciler canonicalizes the Message-ID first (strip outer whitespace; wrap
bare ids in `<>`) so bracketed, bare, and padded forms hit the same sent-log
query and completed receipt (`message_id` is always the canonical form).
Interior whitespace or control characters are rejected (`UNKNOWN`, no Gmail
call) so they cannot split the `q=` search. Then it queries
`users.messages.list(q='in:sent rfc822msgid:<Message-ID>')`:

| Matches | Result | Reasoning |
|---------|--------|-----------|
| 1 | `COMPLETED` | message landed; return Gmail message object + canonical `message_id` |
| 0 | `UNKNOWN` | indexing lag — never authorizes blind retry (`NOT_EXECUTED`) |
| 2+ | `UNKNOWN` | duplicate may already have occurred |
| missing / empty / whitespace-only / interior-ws / control-char ref | `UNKNOWN` | no query made |

Like all reconcilers, `GmailReconciler` is strict about indexing lag: zero matches means "not yet visible," not "never sent." The transition stays hard-blocked so an operator releases it when the provider confirms.

**Consumer Gmail constraint:** the Gmail API may rewrite the MIME Message-ID at
send (e.g. to `…@mail.gmail.com`). A pre-transport `rfc822msgid:` lookup can
then always miss → reconciler stays `UNKNOWN` (fail-closed; not a defect).
Operator release is still required on that account class.

#### Provider-adapter conformance and signed reports

A false `NOT_EXECUTED` verdict is authority to run a consequential operation
one more time. Before shipping a reconciler, run Mycelium's adversarial
conformance kit. The shipped Gmail fixture covers exactly-one and zero matches,
provider indexing lag, ambiguous errors/responses, duplicate matches, malformed
handles, false `NOT_EXECUTED`, and forbidden provider writes:

```console
$ export MYCELIUM_ADAPTER_REPORT_SIGNING_KEY='from-your-secret-manager'
$ mycelium providers verify gmail \
    --key-id provider-ci-2026-01 \
    --output gmail-adapter-report.json
$ mycelium providers verify-report gmail-adapter-report.json --json
```

The JSON report is HMAC-SHA256 signed and binds the suite version, adapter
version, SHA-256 of the adapter source, every case result, timestamp, and signer
key id. `verify-report` rejects a bad signature, failed/missing case, old suite,
or report whose source hash no longer matches the installed adapter.

For another provider, implement `ProviderConformanceFixture`: supply a valid
handle, malformed handles, an entry factory, adapter source bytes, and a
provider-specific scripted client that consumes `ProviderObservation` values
and records every read/write in `ProviderCallAudit`. Then call
`create_adapter_verification_report(...)`. The generic runner decides the
outcome; an adapter cannot receive verified status if uncertain evidence
returns `NOT_EXECUTED` or the fixture observes a write.

This report verifies synthetic adapter behavior, not the live provider account.
Production credentials must still be restricted to read-only provider scopes;
the report states this limitation explicitly.

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

`retry_only_with_same_provider_idempotency_key` (the default for
`keyed_mutate`) means "a retry is safe *only if* it reuses the same provider
idempotency key so the provider dedupes." Declare which kwarg carries that key:

```yaml
tools:
  send_payment:
    side_effect_class: keyed_mutate          # retry_only_with_same_provider_idempotency_key
    provider_idempotency_key_param: idempotency_key
    # optional override (default true for keyed_mutate + declared param):
    # propagate_effect_id_as_provider_key: false
```

or in code: `ToolTransitionBinding.for_tool(..., provider_idempotency_key_param="idempotency_key")`.

With it declared:

- If the first attempt omits the kwarg, Mycelium injects `effect_id` as the
  provider idempotency key after `ATTEMPTING` is recorded and before the tool
  body runs.
- The injected/explicit key is persisted on the ledger entry and reused on
  retries; Mycelium does not derive a fresh key per retry.
- If the call supplies a key explicitly, that explicit value wins and is then
  enforced on subsequent retries.

On a retry of a transition that failed before the effect:

| Incoming key vs stored key | Gate |
|----------------------------|------|
| same key | `ALLOW` (retry proceeds; provider dedupes) |
| different key | `HARD_BLOCK` (would risk a second, undeduped effect) |
| missing on either side | `HARD_BLOCK` |

Add `provider_idempotency_key_ttl` (seconds) when you know the provider's
dedupe window. Same-key `FAILED_BEFORE_EFFECT` retries harden to `HARD_BLOCK`
after expiry. Declaring **both** param and TTL also unlocks same-key retry on
`UNKNOWN` while the window is still valid — Reconciler / operator release stay
preferred; after expiry Mycelium fails closed (no blind double-spend). Omit the
TTL to keep today's behaviour (`UNKNOWN` always hard-blocks).

```yaml
tools:
  send_payment:
    side_effect_class: keyed_mutate
    provider_idempotency_key_param: idempotency_key
    provider_idempotency_key_ttl: 86400   # match provider key lifetime
```

The declared key is excluded from the transition-key fingerprint, so a retry
that swaps the key still resolves to the *same* transition and is caught rather
than silently starting a new one. Declaring
`propagate_effect_id_as_provider_key: true` without
`provider_idempotency_key_param` is rejected at config/binding construction.

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

Small deployments can authenticate releases without an enterprise identity
provider by assigning each operator a secret token (load tokens from environment
variables or a secret manager, never source control):

```python
import os
from mycelium import ActionLedger, StaticTokenOperatorAuthorizer

authorizer = StaticTokenOperatorAuthorizer({
    "ops@example.com": os.environ["MYCELIUM_OPS_TOKEN"],
})
ledger = ActionLedger(storage=storage, operator_authorizer=authorizer)
ledger.release(request_id, verified="not_executed",
               by="ops@example.com", reason="provider shows no charge",
               credential=presented_token)
```

When an authorizer is configured, a missing, incorrect, mismatched, or
authorizer-error credential fails closed before the ledger transition. The
interface is pluggable so an application can replace static tokens with SSO or
another identity service later. This does not protect direct backend writes;
keep ledger write credentials away from operator accounts.

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

> **Note:** YAML / `mycelium init` default is `reclaim_requires_death_signal: true`.
> When off, `release` and reclaim proceed on lease expiry alone (weaker). Direct
> `ActionLedger(...)` without the flag still defaults off for backward compat —
> pass `reclaim_requires_death_signal=True` or use YAML. Redis also keeps a
> durable **tombstone** so TTL eviction of an in-flight key cannot look like a
> brand-new first claim.

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
  missing_run_id_policy: error   # warn (default) | error
  consecutive_soft:
    read: 5
    idempotent_mutate: 3
    keyed_mutate: 2
    non_idempotent_mutate: 2
    irreversible: 2
```

LoopGuard and ScopeGuard key state by `run_id` (fallback `thread_id` for
grouping only). Missing identity **warns and skips** by default so existing
callers keep working. Production should set `profile: production` (or
`missing_run_id_policy: error`) so an enabled guard cannot silently run
unprotected. A valid `run_id` is a
non-empty host-generated string, identical for every step, retry, checkpoint
restore, and worker redispatch of one logical run. Do not mint a random id per
tool call.

```python
run_id = server_run.id  # created once by the host; reuse on every step

with execution_scope(
    TransitionScope(
        thread_id=conversation.id,
        run_id=run_id,
    )
):
    agent.run(...)
```

`thread_id` may span multiple runs and is not a substitute in `error` mode.
`tool_call_id` / transition `request_id` identify one tool operation, not the
run. LangGraph's adapter copies an existing framework `run_id` into
`execution_scope` — do not pass a duplicate when the adapter already provides
one.

Wrapper order: `@secret_args` → `@entity_guard` → `@destructive_confirm` →
`@state_authority` → `@scope_guard` → `@budget_guard` → `@loop_guard` →
`@ledger` → `@bounded` → `@protect` → `func`.

### Budget guard (unnumbered)

Loop guard stops *identical* action thrash. Budget stops **total burn** when
every call is different — including pure LLM chat loops with no tools.

LangGraph budget instrumentation is wired automatically. For CrewAI, wrap the
LLM once with `instrument_crewai_llm`; do not call
`BudgetGuard.check("llm")` or `record_usage()` yourself. Mycelium checks step / token / cost / time **before**
the provider call and records usage **once** from response metadata
(`extract_token_usage`). Streaming aggregates chunks and records when the
stream completes or closes — an incomplete stream is never treated as zero
usage.

| Meter | What it limits | Needs usage metadata? |
|-------|----------------|------------------------|
| `max_steps` | Protected tool invocations + instrumented LLM turns | No |
| `max_duration` | Wall clock since run start | No |
| `max_tokens` | Sum of input+output tokens | Yes (adapter) |
| `max_usd` / `max_cost_usd` | Host-reported USD | Yes (cost resolver) |

```yaml
integrations:
  langgraph:
    enabled: true

budget:
  storage: file
  path: ./mycelium-budget.json
  max_steps: 30
  max_tokens: 100000
  max_cost_usd: 10
  missing_usage_policy: error   # warn is the library default
```

`max_steps` is a run-wide protected-call ceiling, not a count of business
outcomes or high-level workflow stages. Each budget-guarded tool invocation and
instrumented LLM turn reserves one step, including calls on failure, retry, and
cleanup paths. Estimate the legitimate worst case across those paths and keep
business limits—such as candidates processed, attempts, or successful
submissions—in separate application counters. Doctor reports this counting unit
but does not guess whether the ceiling is large enough because the tool list
does not reveal the workflow's possible call paths.

```python
from mycelium import load_config

cfg = load_config("mycelium.yaml")
# chat_model.invoke(...) / .ainvoke / .stream are gated automatically.
```

`missing_usage_policy: warn` (default) emits one warning per run and does
not invent token counts. `error` marks the run's LLM accounting unknown and
blocks later LLM calls (`BudgetAccountingError`). `profile: production`
requires `error` whenever token/cost limits are set, verifies an
**explicitly selected** LLM adapter at startup
(`integrations.langgraph.enabled: true` or
`register_llm_budget_adapter` — having LangGraph installed is not
enough), and rejects `max_usd` unless a cost resolver is registered
(`register_llm_cost_resolver`). Step/time-only budgets may run
without token metadata.

Custom providers only: `wrap_llm_callable` / `instrument_llm` /
`register_llm_budget_adapter` / manual `check("llm")` + `record_usage()`.
Official LangGraph/LangChain integrations do not need those calls.
`check()` and `@budget_guard` reserve one step automatically. Use
`record_usage()` for observed token/USD usage without passing `steps`; combining
the default check/decorator behavior with `record_usage(steps=...)` double-meters
the run and emits a warning. A fully manual host that owns step accounting may
use `check(..., increment_steps=False)` before reporting steps explicitly.
Soft warn (`warn_at`) → `warnings.warn` once per dimension; the step **still
runs**. Hard → `LedgerHardBlockError` only at the declared ceiling (never kill
mid-flight). Operator: `mycelium budget status|release` (`clear` /
`allow-once` / `abort-run`). Status exposes deterministic `remaining_budget`
(pitch word: runway). `get_state()` and `remaining_budget()` both resolve the
active execution scope when called without a key. Outside that scope, pass the
run key explicitly; `remaining_budget() is None` means no scope was resolvable,
not that the run was hard-blocked.

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

### Secret-in-args (AF-010)

Raw credentials must not reach the tool boundary. Pass **references**, not
secrets:

```yaml
secret_args:
  enabled: true
  policy: error          # error | redact | warn
  allow_fields: []       # weaken protection; scope per tool, not globally
  allow_tools: []
  entropy_detection: true

tools:
  charge:
    side_effect_class: non_idempotent_mutate
    secret_fields: [api_key]   # may hold secret://… refs
```

```python
from mycelium import register_secret_resolver

register_secret_resolver(lambda ref: vault.get(ref))  # host-owned; none is built in
charge(api_key="secret://stripe/production/api-key")
```

`policy: error` raises `SecretInArgsError` **before** ledger claim, argument
fingerprinting, receipts, execution, or telemetry. `redact` may pass a
redacted copy only when that cannot change required tool semantics;
otherwise it fail-closes. `warn` keeps compatibility in development and
still sanitizes every persisted or emitted representation. Production
consequential tools require `error`. Omitted `secret_args:` keeps existing
behavior.

Fail-closed pre-execution blocking is the primary protection. Redaction of
Mycelium-owned evidence (ledgers, receipts, outcomes, Doctor/Verify/CLI
JSON, structured logs) is defense-in-depth. Mycelium **cannot** sanitize
logs created inside arbitrary application or provider code after a
resolved value is handed over. `allow_fields` / `allow_tools` weaken the
guard — keep them empty or tool-narrow.

`mycelium doctor` reports whether scanning is enabled, whether production
consequential tools fail closed, whether a resolver is registered, and
labels host logs / third-party providers `not_verifiable`.
`mycelium verify --scenario secret-in-args` searches generated artifacts
for synthetic credentials.

### Entity / destination guard (unnumbered)

A write may contain sensitive data, but it can only cross into a destination
the host explicitly authorized. Unknown destination means no execution. This
is a tool-boundary allowlist, not prompt scanning.

```yaml
entity_guard:
  enabled: true
  missing_policy: error

  tools:
    send_email:
      destinations:
        - path: recipient
          type: email
          allow:
            addresses: [billing@customer.com]
            domains: [customer.com]
        - path: cc
          type: email
          allow: []
          required: false
    http_post:
      destinations:
        - path: url
          type: https_url
          allow:
            hosts: [api.stripe.com, hooks.slack.com]
          reject_redirects: true
    create_ticket:
      destinations:
        - path: project_id
          type: entity_id
          allow:
            values: [SUPPORT, INCIDENTS]
```

Each consequential write tool declares which argument is the destination.
Mycelium canonicalizes it (lowercase email domains/hosts, parsed https URL,
normalized ids) and checks every recipient — `to` / `cc` / `bcc`, webhook
URLs, redirect targets, and nested destination fields. Missing, malformed,
dynamic, undeclared, or unapproved destinations raise `EntityGuardError`
before ledger claim. Canonical destinations are bound into the operation
fingerprint so a retry cannot change recipients on the same `request_id`.
Evidence records tool, destination class or approved entity id, policy
version, and decision — never the payload. Allowlists are host-controlled;
the model cannot add recipients. Omitted `entity_guard:` keeps existing
behavior. Production requires `missing_policy: error`.

`mycelium doctor` reports whether destination policy is enabled and which
consequential tools lack a declaration.
`mycelium verify --scenario entity-guard` proves unauthorized destinations
never claim.

#### Host-selected destinations per run

Static YAML is for destinations known when the application is configured. If a
trusted orchestrator selects an approved repository, page, tenant, or other
exact destination at run start, create an immutable `EntityGuardPolicy` from
that trusted selection and compose it outside the already-ledgered tool with
`apply_decision_policy`:

```python
from mycelium import DecisionPolicyBundle, apply_decision_policy
from mycelium.entity_guard import (
    DEST_ENTITY_ID,
    DestinationAllow,
    DestinationSpec,
    EntityGuardPolicy,
    ToolDestinationPolicy,
)

policy = EntityGuardPolicy(
    policy_version=f"candidate:{candidate.id}:{candidate.approval_revision}",
    tools={
        "contribute": ToolDestinationPolicy(
            destinations=(
                DestinationSpec(
                    path="repository",
                    dest_type=DEST_ENTITY_ID,
                    allow=DestinationAllow(values=frozenset({candidate.repository})),
                ),
                DestinationSpec(
                    path="page_id",
                    dest_type=DEST_ENTITY_ID,
                    allow=DestinationAllow(values=frozenset({candidate.page_id})),
                ),
            )
        )
    },
)
safe_contribute = apply_decision_policy(
    ledgered_contribute,
    DecisionPolicyBundle(entity_policy=policy, consequential=True),
    tool_name="contribute",
)
```

The host must fetch and validate the candidate's approval and canonical IDs
outside the agent. Raw queue content and model output are data, not authority;
the model must never create, extend, or widen this policy. Issue a fresh policy
for a different candidate. If approval can be revoked mid-run, combine this
snapshot with use-time currency or an authority window so authorization is
rechecked at the final boundary. If trusted selection or current approval cannot
be proven, do not expose the write tool. This keeps dynamic routing exact without
using a broad static allowlist.

### Destructive confirm (AF-011)

Tool permission is not object authorization. A configured destructive
tool — refund, delete, cancel, settle, revoke, terminate, purge,
overwrite — may claim or execute only when the host has granted **this
exact operation** on **this exact canonical object**, in this scope/run
when bound, before expiry, for at most `max_uses`. The model cannot
create, widen, renew, or approve a grant. Dual control is intentionally
not implemented; teams that need two-person approval must enforce it in
the host workflow **before** grant issuance.

```yaml
destructive_confirm:
  enabled: true
  missing_policy: error

  tools:
    refund_payment:
      operation: refund
      object:
        type: payment
        id_from: payment_id
      grant:
        bind_request_id: true
        max_uses: 1
        ttl_seconds: 300
    delete_file:
      operation: delete
      object:
        type: file
        id_from: file_id
      grant:
        bind_request_id: true
        max_uses: 1
        ttl_seconds: 120

authority_window:
  enabled: true
  use_time_check: required
  clock_skew_tolerance_seconds: 0
```

Authorization is checked again immediately before use. Expiry at or before
the use instant (`now >= expires_at`) blocks execution; completed-result
reads remain available. Mycelium cannot guarantee authority remains valid
throughout an external network call. Clock synchronization is an
operational assumption unless storage/server time is authoritative.
Authority expiry and use-time currency are separate checks and ship together:
the former validates time-bounded authority, while the latter revalidates the
facts behind the decision.

```python
from mycelium import (
    TransitionScope,
    destructive_grants,
    execution_scope,
    issue_destructive_grant,
)

grant = destructive_grants.issue(
    operation="refund",
    object_type="payment",
    object_id=payment.id,
    request_id=request.id,
    expires_in=300,
)

with execution_scope(
    TransitionScope(run_id=server_run.id, destructive_grants=(grant,))
):
    agent.run(...)
```

Do not infer destructiveness from tool names. Production protection
depends on this explicit configuration or a trusted
`side_effect_class: irreversible` declaration (production fails startup
if an irreversible tool is undeclared). Grants are minted only through
`issue_destructive_grant` / `destructive_grants.issue` — never accepted
as a model-generated dictionary. Object type, object id, and operation
are canonicalized before compare; custom canonicalizers register with
`register_destructive_object_canonicalizer` and run before claim.

Enforcement runs after ordinary argument validation and before ledger
claim, lease, tool body, provider call, or any side effect. A missing,
expired, exhausted, mismatched, or unverifiable grant raises
`DestructiveGrantError` and does not create a claim. Canonical
operation and object identity are bound into the ledger fingerprint so
the same `request_id` with a changed target fails closed. A retry with
the same stable `request_id`, operation, object, and meaningful
arguments reuses the ledger result and does not consume a second grant
use. If the first attempt is ambiguous after a possible provider
crossing, a fresh grant does not authorize a second body — use
reconcile or hard-block resolution.

For `keyed_mutate` tools, reuse the same stable business `request_id` as
the provider idempotency key where the provider supports it and the
tool declares a safe mapping. Mycelium does not auto-inject
provider-specific idempotency parameters. A grant authorizes an
attempt; it does not replace provider idempotency or prove the
provider's final outcome. Mycelium does not claim exactly-once
external effects.

Grant storage: `memory` (dev/test only), `file` / `sqlite` (single-node),
`redis` / `postgres` (multi-worker). Production rejects memory.
Multi-node production rejects file/sqlite. Omitted
`destructive_confirm:` keeps existing behavior.

`mycelium doctor` checks that configured tools identify an exact object,
that production is fail-closed and durable, and labels what cannot be
proven from configuration: host call sites actually mint grants, the
human approval process, provider idempotency, and external side
effects. Installed adapters are not treated as wired.
`mycelium verify --scenario destructive-confirm` proves ungranted
objects never claim.

### Authority-window expiry

Time-bounded authority must still be valid at the side-effect boundary.
Mycelium validates at authorize and again at use (after lease / queue /
backoff, before `mark_maybe_crossed` / provider / body). `now >=
expires_at` raises `AuthorityExpiredError`. Skew tolerance never extends
expired authority. Completed ledger RETURN does not need fresh
authority. Omitted `authority_window:` keeps timeless paths unchanged;
configured `destructive_confirm:` still enforces use-time expiry.
Pairs with AF-012 use-time currency for the full batch guarantee.
`mycelium verify --scenario authority-window`.

### Use-time currency (AF-012)

Decide-time truth is not execute-time authority. Facts the agent used to
decide (refundability, ownership, inventory, policy revision, price) must
still be current at the side-effect boundary. Hosts declare required facts
and register validators; Mycelium authorizes/binds before claim and
revalidates at use (after lease/backoff, before `mark_maybe_crossed` /
body). Stale (`age >= max_age_seconds`), changed, missing, or unverifiable
facts raise `UseTimeCurrencyError` — no body, no provider call, no
`maybe_crossed`. Prompt scanning is not used. Completed ledger RETURN does
not revalidate. Provider preconditions (ETag / If-Match) may be declared
explicitly; local revalidation cannot eliminate a fact change during a
remote network call. Omitted `use_time_currency:` keeps existing behavior.
Production requires `missing_policy: error`. Together with authority-window
expiry, this completes the five-item side-effect guardrails batch.
`mycelium verify --scenario use-time-currency`.

```yaml
use_time_currency:
  enabled: true
  missing_policy: error
  tools:
    refund_payment:
      facts:
        - name: payment.refundable
          subject: {type: payment, id_from: payment_id}
          validator: payment_state
          require: {value: true}
          revision_from: payment_version
          max_age_seconds: 30
          bind_request_id: true
```

```python
from mycelium import register_use_time_validator, use_time_facts, ValidatorResult

def payment_state(*, fact, subject_id, **_):
    payment = load_payment(subject_id)  # host authoritative source
    return ValidatorResult(
        current=payment.refundable,
        value=payment.refundable,
        revision=str(payment.version),
    )

register_use_time_validator("payment_state", payment_state)
use_time_facts.capture(
    name="payment.refundable",
    subject_type="payment",
    subject_id=payment.id,
    value=True,
    revision=payment.version,
    max_age_seconds=30,
    request_id=request_id,
    require_value=True,
)
```

### Scope guard (AF-008): freeze run tool allowlist

AF-008 is when a narrow grant **widens mid-run** (handoff, dynamic
`registry.allow`, new tools injected). `@bounded` still owns per-call
entity/path/output. Scope guard only freezes **which tools this run may
call** and re-checks every step.

```yaml
scope_guard:
  storage: file
  path: ./mycelium-scope.json
  missing_run_id_policy: error   # warn (default, skip if no run_id) | error
  # allowed_tools: from_registry   # default → registry.allowed / tools:
  # on_violation: soft             # soft | hard
```

```python
from mycelium import ScopeGrant, ScopeGuard, scope_guard_sync, execution_scope
from mycelium.transition import TransitionScope

guard = ScopeGuard(default_grant=ScopeGrant(allowed_tools=frozenset({"fetch_customer"})))

@scope_guard_sync(guard)
def fetch_customer(customer_id: str) -> str:
    return customer_id

with execution_scope(TransitionScope(run_id="r1", thread_id="t")):
    fetch_customer(customer_id="c1")  # ok; tools outside the freeze soft-block
```

Wrapper order: `@secret_args` → `@entity_guard` → `@destructive_confirm` →
`@state_authority` → `@scope_guard` → `@budget_guard` → `@loop_guard` →
`@ledger` → `@bounded` → `@protect`. CLI: `mycelium scope status|bind`. Demo:
`python examples/scope_guard_allowlist.py` (from `sdk/`).

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

Supported framework terminals are wired automatically. Configure the checklist
in YAML; do not call `complete_run()` on LangGraph `invoke` / `ainvoke` /
`stream` or CrewAI `kickoff` / `kickoff_async` / `akickoff`. LangGraph
intermediate stream chunks are yielded as they arrive. The
**last** (terminal) chunk is withheld until the contract passes.
Unmarked **required** items refuse (`CompletionRefusedError`, the
terminal chunk is never emitted); unmarked **optional** items warn and
allow. Partial / cancelled streams skip the gate.

```yaml
integrations:
  langgraph:
    enabled: true

completion:
  storage: file
  path: ./mycelium-completion.json
  required:
    - id: charge_customer
    - id: send_receipt
```

```python
from mycelium import load_config

cfg = load_config("mycelium.yaml")
# graph.invoke(...) is gated automatically — no complete_run() call.
cfg.mark_completion("charge_customer", "success", scope_key=run_id)
```

For CrewAI, select its integration and optionally bind the completion scope to
an existing kickoff input:

```yaml
integrations:
  crewai:
    enabled: true
    run_id_from: workflow_id
```

On a successful CrewAI execution, Mycelium checks the active completion
contract before the result returns. A refusal becomes CrewAI's fail-closed
`HookAborted` signal, with the Mycelium completion error preserved as its cause.
Failed executions do not attempt to report successful completion.

`profile: production` verifies an **explicitly selected** terminal adapter
at startup. Having LangGraph or CrewAI installed is not enough; enable the
matching integration explicitly. For a custom runtime launched with
`mycelium run`, declare an idempotent installer in the configuration:

```yaml
completion:
  adapter_installer: my_app.mycelium_completion:install
  required:
    - id: charge_customer
```

The installer runs in the application process after its import path is ready.
It must wire the real final-message/terminal boundary and then call
`register_terminal_adapter("custom")`. If `completion:`
is enabled but no adapter was selected, load raises `ConfigError` so the
app cannot look protected while checks are bypassed. Development mode
warns and keeps the manual fallback.

Custom runtimes only: `wrap_final_message`, `gate_graph_end`,
`complete_run`, or the configured `adapter_installer`. Applications that load
configuration themselves may still register an adapter before `load_config`.
Official LangGraph integrations do not need those calls.

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

Wrapper order: `@secret_args` → `@entity_guard` → `@destructive_confirm` → `@state_authority` → `@scope_guard` →
`@loop_guard` → `@ledger` → `@bounded` → `@protect` → `func`.

`decision_id` / `state_ref` are bookkeeping kwargs (excluded from the args
fingerprint) and are stored on `LedgerEntry` at claim for audit. Enforcement
stays in `StateAuthority`, not inside claim resolution.

Storage backends:

| Backend | Use case | YAML `storage` |
|---------|----------|----------------|
| `memory` | Tests / single-process disposable state | `memory` (default; not production) |
| `file` | Local development / single host (`fcntl` lock) | `file` + `path` |
| `sqlite` | Durable single-node default (stdlib, no extras) | `sqlite` + `path` (+ optional `table`) |
| `redis` | Multi-worker coordination | `redis` + `url` or `url_env` |
| `postgres` | Durable multi-worker / audit-oriented deployment | `postgres` + `dsn` or `dsn_env` |

The action ledger also stores call/result evidence. Read the [ledger payload
storage guide](docs/LEDGER_PAYLOAD_STORAGE.md) before selecting a durable
backend; it lists every serialized field, the backend-specific layout, and the
current redaction and pruning limits.

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
| `warn` | library / development | One-time `UserWarning` per tool when a failed entry is reclaimed; legacy behavior (re-execute) |
| `strict` | omitted YAML value under `profile: production` | Routes through `claim_side_effecting` with a conservative binding (`non_idempotent_mutate`); failed retries **hard-block** instead of re-executing |

```python
# Decorator
@ledger_sync(unclassified_policy="strict")
def my_tool(...): ...

# YAML
action_ledger:
  storage: redis
  url_env: MYCELIUM_REDIS_URL
  unclassified_policy: strict
  on_args_drift: soft   # soft (default) | hard | off — identity-conflict gate
```

When `transition:` is configured and a side-effecting tool uses memory storage, a one-time warning is emitted at YAML load time — the duplicate-side-effect guard only holds within the process. Set `action_ledger.memory_storage_policy: error` to reject that combination at load time (`ConfigError` names the tool and recommends `file/sqlite/redis/postgres`). The default remains `warn` so existing test/dev configs keep loading. Reads may keep using memory storage under either policy.

Durable storage keeps the ledger across a process restart. It cannot by itself tell you whether an external provider finished during the crash window (`maybe_crossed`). Production still needs the full fail-closed pattern:

```yaml
transition:
  reclaim_requires_death_signal: true
action_ledger:
  storage: postgres          # or redis for multi-worker coordination
  dsn_env: MYCELIUM_POSTGRES_DSN
  unclassified_policy: strict
  memory_storage_policy: error
```

```python
@ledger_sync(transition_binding=binding, reconciler=StripeReconciler())
def send_payment(amount, recipient, *, idempotency_key):
    record_external_operation(idempotency_key)   # before the provider call
    with side_effect():                          # maybe_crossed is durable first
        return gateway.charge(amount, recipient, idempotency_key=idempotency_key)
```

Checklist: durable Redis/Postgres ledger, stable request/transition identity, provider idempotency key, `record_external_operation()` before the provider call when possible, `side_effect()` around the external call, a read-only `Reconciler`, `unclassified_policy: strict`, and `reclaim_requires_death_signal: true`. Unresolved ambiguity stays fail-closed — hard-block or reconcile; never re-execute blindly.

### Transition pagination and retention

Postgres action ledgers use a bounded connection pool and status/start/finish
indexes. Redis action ledgers maintain sorted status/time indexes (existing
rows are indexed once on first use), so paginated transition reads no longer
scan the full table or keyspace.

```yaml
action_ledger:
  storage: postgres
  dsn_env: MYCELIUM_POSTGRES_DSN
  pool_min_size: 1
  pool_max_size: 10
  retention_seconds: 2592000  # 30 days; used when prune omits --older-than
```

```bash
mycelium transitions list --postgres-dsn "$MYCELIUM_POSTGRES_DSN" --limit 100
mycelium transitions export --postgres-dsn "$MYCELIUM_POSTGRES_DSN" --output transitions.ndjson
mycelium transitions prune --postgres-dsn "$MYCELIUM_POSTGRES_DSN" --older-than 30d --dry-run
mycelium transitions prune --postgres-dsn "$MYCELIUM_POSTGRES_DSN" --older-than 30d --archive transitions.ndjson --execute
```

Pruning is a dry-run unless `--execute` is supplied. Its safe default includes
only `COMPLETED` and `FAILED_BEFORE_EFFECT`; ambiguous, blocked, expired, and
in-flight records are retained unless explicitly selected with repeatable
`--outcome` flags. Export and archive files are sanitized NDJSON.

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

**Deployments:** set `profile: production`. Direct constructors and development
keep their compatibility defaults; the production profile applies these
fail-closed YAML defaults and requirements:

- `action_ledger.memory_storage_policy: error` — side-effecting tools cannot
  use memory storage
- `action_ledger.request_identity_policy: require_explicit` —
  `idempotent_mutate` / `keyed_mutate` / `non_idempotent_mutate` /
  `irreversible` tools need a host-owned `request_id` (or
  `request_id_from`) before claim or execution. Reads may keep derived
  identity. `tool_call_id` / `run_id` / `thread_id` are not business IDs.
- omitted `action_ledger.unclassified_policy` defaults to `strict`, so a
  failed retry without a transition binding hard-blocks instead of silently
  reclaiming. An explicit `warn` remains accepted for compatibility; declare
  `side_effect_class` for consequential tools.
- `loop_guard` / `scope_guard` `missing_run_id_policy: error` when those
  guards are enabled — missing `run_id` raises `MissingRunIdentityError`
- `outcome_emit:` is required with durable storage (not memory). Prefer
  `storage: postgres` for distributed deployments. `storage: file` is
  single-node only. `storage: redis` needs `persistence: required` (AOF
  or equivalently durable Redis; Mycelium cannot verify the server).
  Emission failure is fail-closed.
- `secret_args.policy: error` when `secret_args:` is enabled and
  consequential tools exist. Weaker `warn` / `redact` under production
  is rejected. Omitted `secret_args:` stays backward compatible.
- `entity_guard.missing_policy: error` when `entity_guard:` is enabled.
  Omitted `entity_guard:` stays backward compatible.
- `destructive_confirm.missing_policy: error` when `destructive_confirm:`
  is enabled. Production grant storage must be durable (not memory).
  Multi-node production requires redis or postgres. Irreversible tools
  must be declared. Omitted `destructive_confirm:` stays backward
  compatible.

Explicit weaker settings for memory storage, request identity, run identity,
outcome storage, and enabled production guards are rejected (`ConfigError`),
not silently weakened. `action_ledger.unclassified_policy: warn` is the stated
compatibility exception. Omit `profile` or set `profile: development` for
tests.

The direct/decorator and development default remains
`unclassified_policy: warn`. Production changes only the omitted YAML value to
`strict`; explicit `warn` remains an accepted compatibility choice.

```yaml
profile: production

deployment:
  topology: multi_node   # or single_node; omit → doctor warns

integrations:
  langgraph:
    enabled: true

transition:
  agent_id: my-agent
  policy_version: "2026.08.1"
  reclaim_requires_death_signal: true

action_ledger:
  storage: postgres
  dsn_env: MYCELIUM_POSTGRES_DSN
  unclassified_policy: strict
  memory_storage_policy: error
  request_identity_policy: require_explicit

loop_guard:
  missing_run_id_policy: error

scope_guard:
  missing_run_id_policy: error

completion:
  storage: file
  path: ./mycelium-completion.json
  required: []

budget:
  storage: file
  path: ./mycelium-budget.json
  missing_usage_policy: error

secret_args:
  enabled: true
  policy: error
  allow_fields: []
  allow_tools: []
  entropy_detection: true

outcome_emit:
  storage: postgres
  url_env: DATABASE_URL
  table: mycelium_outcomes
  on_failure: error
  # Single-node alternative:
  # storage: file
  # path: ./mycelium-outcomes.jsonl
  # Redis Streams (requires durable Redis + explicit ack):
  # storage: redis
  # url_env: REDIS_URL
  # key_prefix: mycelium:outcomes
  # persistence: required
```

### Unified durable guard state

Configure one atomic state backend for every stateful guard instead of giving
each guard a separate file or process-local dictionary:

```yaml
state_backend:
  storage: postgres            # memory | file | redis | postgres
  dsn_env: DATABASE_URL
  namespace: payments-prod

loop_guard: {}
scope_guard:
  allowed_tools: [lookup_invoice, send_payment]
completion:
  required: [payment_recorded]
state_flush: {}
audit_receipt:
  signing_key_env: MYCELIUM_AUDIT_SIGNING_KEY
```

When `state_backend` is present, a stateful guard with no `storage` setting is
automatically placed in its own namespace on that backend. This also applies to
any of these five guards you enable later; no config duplication is needed. Use `storage: shared` to
make the choice explicit, or set a guard's own `storage` to retain a legacy
backend. `memory` is development-only, `file` is durable for one node, and
Redis/Postgres provide multi-worker atomic compare-and-swap updates.

A completely new guardrail type still needs a small typed adapter that defines
how its state is serialized and atomically updated. It can reuse
`NamespacedAtomicStorage`; the backend implementations themselves do not need
to change.

To move existing guard files or per-feature Redis/Postgres state safely:

```console
$ mycelium state migrate --plan --config mycelium.yaml
$ mycelium state migrate --apply --config mycelium.yaml
$ mycelium doctor --config mycelium.yaml --strict
```

During the copy, keep each old feature's `storage` configuration and add the
new top-level `state_backend`. Stop workers so the source does not change, run
the plan and apply commands, then remove each feature's `storage`/`path` (or set
`storage: shared`) and restart. Migration never deletes or overwrites the old
records, so rollback is switching the feature back to its old storage. It
refuses conflicting destination records instead of guessing which copy wins.

### `mycelium doctor` (verify protection is real)

Installing Mycelium does not prove a deployment is protected. `mycelium doctor`
is a **read-only by default** verifier for configuration and detectable wiring:

```console
$ mycelium doctor --config mycelium.yaml
$ mycelium doctor --config mycelium.yaml --fix  # version/schema metadata only
$ mycelium doctor --config mycelium.yaml --strict --json   # CI gate
```

It checks profile defaults, tool classification, business request identity,
durable ActionLedger / outcome backends, the shared state backend, run-id guard policies, completion and
budget adapter selection, secret-in-args scanning / fail-closed production,
destination-policy coverage,
and optional `deployment.topology`. It never executes
application tools, never calls an LLM, never writes ledger/outcome rows, and
does not repair runtime policy. The opt-in `--fix` mode only adds an explicit
`config_version`, a YAML editor schema hint, and a local JSON Schema sidecar. It
does not guess tool classifications, storage, credentials, or production policy.

Evidence labels distinguish what Mycelium can prove (`statically_verified`,
`runtime_registration_verified`, `connectivity_verified`) from what remains an
**operator assertion** (for example Redis AOF) or is **not verifiable** from
config alone (call-site `request_id` / `run_id` binding). Doctor does not
replace integration tests or fault injection.

### `mycelium verify` (exercise the guarantees)

Doctor inspects configuration. `mycelium verify` empirically tests Mycelium’s
production guarantees against the configured storage backend using **synthetic
operations only**:

```console
$ mycelium verify --config mycelium.yaml --scenario redispatch
$ mycelium verify --config mycelium.yaml --scenario all --strict --json
```

Scenarios: `redispatch`, `contention`, `storage-outage`, `worker-crash`,
`ambiguous-effect`, `reconcile`, `secret-in-args`, `entity-guard`,
`destructive-confirm`, `authority-window`, `use-time-currency`, `simulation`,
or `all` (that order). The durable-backend-only `simulation` scenario sweeps
crash boundaries, checks the at-most-one-COMMITTED invariant, and proves that a
takeover fence rejects the superseded worker. Verify never executes
application tools, never calls an LLM, never contacts a real business provider,
and never inspects or alters existing production transitions. Test data uses a
unique `mycelium:verify:<uuid>:` namespace and is deleted unless
`--keep-artifacts` is set.

Passing Verify is strong deployment evidence, not proof of every external
system. Redis persistence remains operator-asserted. Host business-identity
authority remains an application responsibility. `empirically_verified` is true
only when every selected scenario passed; Doctor `operator_asserted` /
`not_verifiable` evidence is never promoted to “proven.” File and SQLite are
labeled single-node; PostgreSQL is the recommended distributed backend.

#### Optional cluster verification

Cluster verification is deliberately separate and is never run by ordinary
`mycelium verify --scenario ...` commands. Enable it only in a test/change-approval
environment with a shared Redis or PostgreSQL ledger and an external provider's
**sandbox** API:

```yaml
deployment:
  topology: multi_node

verify:
  cluster:
    enabled: true
    provider:
      adapter: http_json
      name: payments-sandbox
      sandbox: true
      base_url_env: PAYMENTS_SANDBOX_URL
      token_env: PAYMENTS_SANDBOX_TOKEN  # optional
      timeout: 5
    attestation:
      signing_key_env: MYCELIUM_DEPLOYMENT_ATTESTATION_KEY
      key_id: ci-2026-08
```

```console
$ export PAYMENTS_SANDBOX_URL=https://sandbox.example.test/mycelium-verify
$ export MYCELIUM_DEPLOYMENT_ATTESTATION_KEY='a CI-managed secret'
$ mycelium verify --config mycelium.yaml --cluster --strict --json \
    --attestation-output deployment-attestation.json
$ mycelium verify --verify-attestation deployment-attestation.json \
    --attestation-key-env MYCELIUM_DEPLOYMENT_ATTESTATION_KEY --json
```

The built-in `http_json` adapter uses a narrow sandbox contract:

- `PUT /operations/{operation_id}` executes an idempotent sandbox operation.
- `GET /operations/{operation_id}` is read-only and returns `404` when absent.
- A completed response has JSON `status` equal to `completed`, `complete`,
  `succeeded`, or `success`.

The verifier creates a unique ledger namespace, launches two subprocess workers,
interrupts their backend connection through a verifier-owned TCP proxy, restores
it, lets worker A reach the sandbox provider, hard-kills A, and requires worker B
to reconcile the recorded operation without executing the provider body again.
It signs a versioned attestation containing the config digest and every check.
Secrets and provider tokens are read from environment variables and are not
included in the attestation.

`--keep-artifacts` is for debugging: it deliberately makes the signed cleanup
check fail because namespaced backend evidence was retained. The sandbox
operation itself is not deleted; use a sandbox with automatic test-data expiry.

The command refuses to start unless all opt-ins and prerequisites are present:
`verify.cluster.enabled: true`, `deployment.topology: multi_node`, Redis or
PostgreSQL, `provider.sandbox: true`, a sandbox URL, and an attestation key plus
key id. Use only a private test backend. The built-in fault proxy rejects
`rediss://` and hostname-verifying PostgreSQL TLS modes because rewriting those
endpoints would invalidate their certificates. Normal Verify remains synthetic
and does not contact any provider.

**Minimum integration (3 steps):**

```yaml
# mycelium.yaml: global sections (configure once)
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"
  lease_ttl: 3600
  # lease_renew_interval: 1200   # default = lease_ttl/3; 0 disables auto-renew
  reclaim_requires_death_signal: true    # YAML default; false = lease-expiry-as-death
  # presumed_dead_after: 7200             # default = 2 × lease_ttl; grace window for heartbeat

action_ledger:
  storage: sqlite
  path: ./mycelium-ledger.db
  unclassified_policy: strict   # warn (default) or strict
  memory_storage_policy: warn   # warn (default) | error
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
  missing_run_id_policy: error

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

**Solution:** Every claim receives a monotonically increasing fence. Every
subsequent mutation goes through `try_transition`, which checks the stored
terminal outcome plus the expected fence (and owner/effect state where
applicable). Already-resolved or superseded entries refuse overwrites.

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

### Fenced mutations

The `@ledger` / `@ledger_sync` wrapper captures the current worker identity and
claim fence. A takeover increments the stored fence; stale decision, boundary,
heartbeat, provider-reference, receipt, completion, failure, reconciliation,
and operator-resolution writes are rejected with
`LedgerOutcomeAlreadySetError`. Owner checks remain additional protection. In
`_record_failure`, a CAS rejection never masks the original tool exception.

### Backend implementation

| Backend | CAS mechanism |
|---------|--------------|
| Memory | `InMemoryLedgerStorage` delegates to `set()` when CAS matches |
| File | Within `LockedJsonDictFile.read_modify_write` |
| SQLite | Conditional `UPDATE` over JSON outcome / owner / fence / effect state |
| Redis | `pipe.watch()` on the key; `WatchError` retry loop on conflict |
| Postgres | Conditional `UPDATE ... RETURNING` over outcome / owner / fence / effect state |

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
from flat append-only rows. Off by default in development. `profile:
production` requires `outcome_emit:` with durable storage (not memory).
**Postgres** is the recommended distributed durable backend. **File** is
single-node only. **Redis** uses Streams and is accepted in production only
with `persistence: required` (you must enable AOF or an equivalently durable
Redis deployment; Mycelium cannot independently verify that). Backend
outages fail closed in production (`on_failure: error`).

Enable it in YAML and every ledgered tool starts emitting:

```yaml
outcome_emit:
  storage: postgres             # recommended multi-node durable
  url_env: DATABASE_URL         # or url: / dsn: / dsn_env:
  table: mycelium_outcomes
  long_running_after: 3600      # seconds (default: lease_ttl)
  on_failure: error             # production default; development defaults to warn
  exporters:                    # optional; Mycelium does not host a dashboard
    - type: opentelemetry       # application's configured MeterProvider
    - type: prometheus          # application's default CollectorRegistry
    - type: webhook
      url_env: OUTCOME_WEBHOOK_URL
      secret_env: OUTCOME_WEBHOOK_SECRET
      timeout: 5
  # storage: file               # durable, single-node only
  # path: ./mycelium-outcomes.jsonl
  # storage: redis              # Streams; not durable unless Redis persistence is on
  # url_env: REDIS_URL
  # key_prefix: mycelium:outcomes
  # persistence: required       # required in production; acknowledgement only
```

Or pass an emitter directly:

```python
from mycelium import OutcomeEmitter, ledger_sync

emitter = OutcomeEmitter(agent_id="acme", storage=FileOutcomeStorage("outcomes.jsonl"))

@ledger_sync(storage=..., transition_binding=..., outcome_emitter=emitter)
def charge(amount):
    ...
```

Rows are flat JSON objects (NDJSON for file storage), emitted only on
resolution events — a dispatch resolving to a gate (`ALLOW` / `RETURN` /
`HARD_BLOCK` / `SOFT_BLOCK`), the tool body starting / completing / failing,
operator releases, final-boundary decision denials, fence rejections, and
lease-renewal failures. Poll ticks never emit. Development emission is
fault-tolerant (storage failures are logged and swallowed). Production
emission is fail-closed: a required durable write failure raises
`OutcomeEmitError` on success paths, and never replaces an existing
tool/provider exception.

### Export outcomes — not a dashboard

Mycelium does not host, query, or visualize these signals. It exports the
same outcome rows to the telemetry system you already operate: OpenTelemetry,
Prometheus, or any HTTP webhook receiver. Keep a durable outcome store as the
fanout primary so DTTR remains replayable:

```python
from mycelium import (
    FanoutOutcomeStorage,
    FileOutcomeStorage,
    OpenTelemetryOutcomeStorage,
    OutcomeEmitter,
    PrometheusOutcomeStorage,
    WebhookOutcomeStorage,
)

storage = FanoutOutcomeStorage(
    FileOutcomeStorage("outcomes.jsonl"),
    OpenTelemetryOutcomeStorage(),       # uses the application's MeterProvider
    PrometheusOutcomeStorage(),          # uses the application's registry
    WebhookOutcomeStorage(
        "https://events.example.com/mycelium",
        secret="rotate-me",             # optional HMAC-SHA256 signature
    ),
)
emitter = OutcomeEmitter(agent_id="acme", storage=storage)
```

Install exporter APIs with `mycelium[observability]`, or install only
`mycelium[opentelemetry]` / `mycelium[prometheus]`. The application remains
responsible for configuring its OTel reader/exporter and exposing its
Prometheus registry. Webhooks receive a versioned `mycelium.outcome.v1`
envelope containing the full sanitized row and the metric points derived from
it; `X-Mycelium-Event-ID` supports dedupe and an optional
`X-Mycelium-Signature: sha256=...` authenticates the raw body.

The fixed metric contract deliberately excludes request IDs, run IDs,
operators, and free-form reasons from metric labels:

| Metric | Type | Meaning |
|---|---|---|
| `mycelium.hard_blocks` | counter | transitions resolved to `HARD_BLOCK` |
| `mycelium.ambiguity.age` | histogram (seconds) | ambiguity age whenever an ambiguous outcome is observed |
| `mycelium.fence_rejections` | counter | superseded owner/fence writes refused |
| `mycelium.lease_renewal_failures` | counter | failed execution-lease heartbeat attempts |
| `mycelium.decision_denials` | counter | final-boundary decisions that denied execution |
| `mycelium.recovery.time` | histogram (seconds) | first observed ambiguity through release or safe resolution |
| `mycelium.operator_releases` | counter | recorded manual reconciliations |

Prometheus exposes dotted names with underscores and adds `_total` to
counters; the two duration histograms are suffixed `_seconds`. To export old
rows after enabling a backend, call `export_rows(storage.list_all(), sink)`.

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
pyright
```

### Typing and Static Analysis

The SDK enforces static typing using Pyright in CI:
- **Baseline Coverage:** Stable public modules, API contracts, transition gates, and decorators are checked in CI (`pyright`).
- **PEP 561 Status:** The package currently defers shipping `py.typed` until internal storage and simulation harness legacy annotations are fully unified.
