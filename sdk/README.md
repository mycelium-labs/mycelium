# Mycelium runtime

## One painful bug → five lines of code

**LangGraph Cloud redispatches a long tool call while the first is still running.** Both complete. You pay twice. Side effects run twice. [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417)

```bash
pip install mycelium-runtime   # Python 3.10+
mycelium init                  # scaffolds mycelium.yaml for your tool
mycelium demo                  # see the bug and the fix (no LangGraph required)
```

```python
from mycelium import ledger_sync

@ledger_sync()
def subagent_task(task: str) -> dict:
    return run_slow_subagent(task)

# Pass tool_call_id from LangGraph; redispatch returns the cached result
subagent_task(task="analyze_market", tool_call_id=call["id"])
```

Or wire from `mycelium init`:

```python
from mycelium import load_config

config = load_config("mycelium.yaml")

@config.apply
def subagent_task(task: str) -> dict:
    return run_slow_subagent(task)
```

## What else it does

| Problem | What Mycelium does |
|---------|-------------------|
| **Stale or broken context** | TTL cache, message repair, history limits; agent sees fresh, valid data |
| **Bad or unauthorized tool calls** | Validate inputs/outputs, allowlists, scoped paths; block before execution |
| **Duplicate side effects on retry** | Idempotency ledgers, state flush on cancel, signed receipts; pay once, not twice |

Framework-agnostic. Raw message lists and plain Python functions (LangGraph, CrewAI, OpenAI tool loops, etc.).

## Install

**Requires Python 3.10+** (3.11+ recommended).

```bash
pip install mycelium-runtime
mycelium init              # quickstart: duplicate-tool fix → ./mycelium.yaml
mycelium init --full       # all guards, commented examples
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

## Quickstart: idempotency & audit receipts

Stop duplicate payments, emails, and API calls when the framework retries. Persist state on cancel. This is **runtime prevention**, not distributed tracing.

### Tool-level idempotency

```python
from mycelium import ledger_sync

@ledger_sync()
def send_payment(amount: float, recipient: str) -> dict:
    return gateway.charge(amount, recipient)

# Same logical call executes only once.
send_payment(amount=100.0, recipient="acct_123", request_id="invoice-42")
send_payment(amount=100.0, recipient="acct_123", request_id="invoice-42")
```

Async tools:

```python
from mycelium import ledger

@ledger()
async def send_payment(amount: float, recipient: str) -> dict:
    return await gateway.charge(amount, recipient)
```

## What `@ledger` / `ledger_sync` do

- Record every tool invocation in a durable `ActionLedger`
- Deduplicate retries and redispatches by `request_id` or LLM `tool_call_id`
- Allow legitimate repeats when the request id differs
- Persist failed attempts for audit and debugging

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
action_ledger:
  storage: file
  path: ./mycelium-ledger.json
  tools: [send_payment]          # auto-ledger side-effect tools

task_ledger:
  storage: file
  path: ./mycelium-task-ledger.json
  tasks: [process_invoice]

state_flush:
  storage: file
  path: ./mycelium-state.json

audit_receipt:
  agent_id: my-agent
  signing_key_env: MYCELIUM_SIGNING_KEY
  storage: file
  path: ./mycelium-receipts.jsonl

# Per-tool: only what differs (schemas, cache, etc.)
tools:
  fetch_customer:
    protect: {entity_param: customer_id, ttl: 60}
    bounded:
      schema:
        customer_id: {type: string, required: true, pattern: "^c\\d+$"}

  send_payment:
    bounded:
      schema:
        amount: {type: number, required: true}
        recipient: {type: string, required: true}

tasks:
  process_invoice:
    ledger: true
    id_from: [invoice_id]

registry:
  auto: true                     # allowlist = all configured tools

history_guard:
  max_tokens: 100000

message_validator:
  enabled: true
```

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
receipts automatically.

Legacy per-tool style still works; run `mycelium init` for the full annotated template.

---

## For contributors (repo layout)

Clone the GitHub repo to run proofs and tests. PyPI installs only the `mycelium` package.

```bash
git clone https://github.com/mycelium-labs/mycelium.git
cd mycelium/sdk && pip install -e ".[dev]"
pytest tests/ -v
```
