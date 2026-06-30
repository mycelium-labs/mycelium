# Mycelium

Runtime failure prevention for AI agents. Keeps context current, enforces tool boundaries, and deduplicates side effects on retry.

Framework-agnostic. Works with raw message lists and plain Python functions (LangGraph, CrewAI, OpenAI tool loops, etc.). Requires Python 3.10 or later.

**PyPI:** [`mycelium-runtime`](https://pypi.org/project/mycelium-runtime/) · **Handbook:** [docs/index.html](docs/index.html) · **API reference:** [sdk/README.md](sdk/README.md)

## What it does

| Problem | Guards |
|---------|--------|
| Stale or broken context | `@protect`, `protect_sync`, `Session`, `MessageValidator`, `HistoryGuard` |
| Bad or unauthorized tool calls | `@bounded`, `bounded_sync`, `ToolRegistry`, `ToolRunner` |
| Duplicate side effects on retry | `@ledger`, `ledger_sync`, `task_ledger_sync`, `StateFlush`, `AuditReceipt` |

## Quick start

The most common entry point: a long tool call gets redispatched while the first is still running. Both complete. Side effects run twice. ([langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417))

```bash
pip install mycelium-runtime
mycelium init
mycelium demo
```

```python
from mycelium import ledger_sync

@ledger_sync()
def subagent_task(task: str) -> dict:
    return run_slow_subagent(task)

# Pass tool_call_id from your framework on each invocation
subagent_task(task="analyze_market", tool_call_id=call["id"])
```

Or wire from YAML:

```python
from mycelium import load_config

config = load_config("mycelium.yaml")

@config.apply
def subagent_task(task: str) -> dict:
    return run_slow_subagent(task)
```

## Install

```bash
pip install mycelium-runtime
pip install 'mycelium-runtime[redis]'      # optional, multi-worker ledgers
pip install 'mycelium-runtime[postgres]'   # optional, SQL audit storage

mycelium init              # quickstart: action ledger for one tool
mycelium init --full       # all guards, annotated template
mycelium init --minimal    # smaller multi-guard template
mycelium demo              # terminal demo of duplicate tool execution
```

## Examples

### Context

```python
from mycelium import protect, Session, MessageValidator, HistoryGuard

@protect(entity_param="customer_id", ttl=60)
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

async def run(customer_id: str, messages: list):
    async with Session():
        await fetch_customer(customer_id=customer_id)
        messages = MessageValidator().repair(messages)
        messages = HistoryGuard(max_tokens=100_000).validate(messages)
        return await llm.ainvoke(messages)
```

Sync tools: use `protect_sync` and `with Session():`.

### Tools

```python
from mycelium import bounded, ToolRegistry, ToolRunner

registry = ToolRegistry(allowed=["fetch_customer"])

@registry.register
@bounded(
    schema={"customer_id": {"type": "string", "required": True, "pattern": r"^c\d+$"}},
    output_schema={"customer_id": {"type": "string"}, "name": {"type": "string"}},
)
async def fetch_customer(customer_id: str) -> dict:
    return await db.get(customer_id)

runner = ToolRunner(registry=registry)
result = await runner.call(fetch_customer, customer_id="c1")
```

### Actions

```python
from mycelium import ledger_sync, task_ledger_sync

@ledger_sync()
def send_payment(amount: float, recipient: str) -> dict:
    return gateway.charge(amount, recipient)

send_payment(amount=100.0, recipient="acct_123", tool_call_id="call_abc")
send_payment(amount=100.0, recipient="acct_123", tool_call_id="call_abc")  # cached

@task_ledger_sync(id_from=["invoice_id"])
def process_invoice(invoice_id: str) -> dict:
    return {"invoice_id": invoice_id, "status": "paid"}
```

Idempotency keys: `tool_call_id`, `request_id`, or `task_id`. Ledger storage: `memory`, `file`, `redis`, `postgres`.

### YAML

```python
from mycelium import load_config
import my_tools

config = load_config("mycelium.yaml")
tools = config.instrument(my_tools)

with config.run(thread_id):
    messages = config.prepare_messages(messages)
```

Global sections in `mycelium.yaml`: `action_ledger`, `task_ledger`, `state_flush`, `audit_receipt`, `tools`, `tasks`, `registry`, `runner`, `history_guard`, `message_validator`. Run `mycelium init --full` for the annotated template.

## Repo layout

```
mycelium/
├── README.md
├── CHANGELOG.md
├── LICENSE
├── docs/
│   └── index.html         # developer handbook (GitHub Pages)
├── .github/workflows/     # CI + PyPI publish
└── sdk/                   # publishable package
    ├── pyproject.toml
    ├── README.md          # full API reference (PyPI long description)
    ├── mycelium/          # import mycelium
    │   └── templates/     # bundled YAML templates (mycelium init)
    └── tests/
```

| Audience | Gets |
|----------|------|
| `pip install mycelium-runtime` | `mycelium/*.py` + `mycelium/templates/*.yaml` |
| GitHub clone | `sdk/` source, tests, handbook in `docs/` |

The PyPI wheel does not include `sdk/tests/`.

## Develop

```bash
git clone https://github.com/mycelium-labs/mycelium.git
cd mycelium/sdk
pip install -e ".[dev]"
pytest tests/ -v
ruff check mycelium tests
```

Publish: tag `v*` triggers PyPI release via GitHub Actions.

## Docs

| Doc | Description |
|-----|-------------|
| [Handbook](docs/index.html) | Install, API, context/tools/actions examples, YAML |
| [SDK reference](sdk/README.md) | Full API, storage backends, `ToolRunner` retry loop |
| [Changelog](CHANGELOG.md) | Release history |

**GitHub Pages:** Settings → Pages → source **main**, folder **/docs**. Serves `docs/index.html` at your repo URL.

## License

MIT. See [LICENSE](LICENSE).
