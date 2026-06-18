# Mycelium SDK

Runtime failure prevention for AI agents. v0 covers context corruption (AF-006), v1 covers tool boundary enforcement (AF-004), and v2 covers the observability black hole (AF-002).

## Install

```bash
pip install ./sdk
```

## Quickstart — AF-006

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

- `@protect` / `protect_sync` — TTL cache with per-entity keys; auto-refetch when stale; clear on error
- `Session` — one cache per agent run; use in production to prevent cross-request leakage

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

## Quickstart — AF-004

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

Field spec keys: `type` (`string`, `integer`, `number`, `boolean`), `required`, `pattern`, `min_length`, `max_length`. You pass plain dicts — Mycelium validates internally; no Pydantic imports in your code.

## What `@bounded` / `bounded_sync` do

- `@bounded` / `bounded_sync` — validate tool args against your field spec **before** the function runs
- `output_schema` — validate the return value **after** the function runs; bad results are not propagated
- `allowed_paths` / `entity_pattern` — user-defined scope gates (path prefixes, entity ID format)
- On failure, raises `ToolBoundaryError` with `llm_message` for the agent loop — does not retry by itself

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

## Quickstart — AF-002

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

```python
from mycelium import ActionLedger, FileLedgerStorage, InMemoryLedgerStorage

ledger = ActionLedger(storage=InMemoryLedgerStorage())
ledger = ActionLedger(storage=FileLedgerStorage("./mycelium-ledger.json"))
```

## Quickstart — AF-002 task-level ledger

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

Declare guards in `mycelium.yaml` instead of sprinkling decorators through your code:

```yaml
# mycelium.yaml
tools:
  fetch_customer:
    protect:
      entity_param: customer_id
      ttl: 60
    bounded:
      schema:
        customer_id:
          type: string
          required: true
          pattern: "^c\\d+$"
      output_schema:
        customer_id: {type: string, required: true}
        name: {type: string, required: true}
      allowed_paths:
        - /workspace/src/

  search_docs:
    bounded:
      schema:
        query: {type: string, required: true}

  send_payment:
    ledger:
      storage: file
      path: ./mycelium-ledger.json

registry:
  allowed:
    - fetch_customer
    - search_docs

runner:
  max_llm_retries: 2
  max_tool_retries: 3

history_guard:
  max_tokens: 100000
  max_messages: 1000

message_validator:
  enabled: true
```

Load it once and apply guards to your plain functions:

```python
from mycelium import load_config

config = load_config("mycelium.yaml")

@config.apply
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

@config.apply
def search_docs(query: str) -> list[str]:
    return docs.search(query)

# Or wrap every configured tool in a module at once:
import my_tools
namespace = config.wrap_module(my_tools)

# Registry, runner, and guards are built from the same config:
registry = config.registry
runner = config.build_runner()
guard = config.build_history_guard()
validator = config.build_message_validator()
```

Mycelium matches tools by function name, detects sync vs async automatically, and
applies validation outside caching so invalid args never pollute the cache.

A complete commented template is available at `sdk/examples/mycelium.template.yaml`.
