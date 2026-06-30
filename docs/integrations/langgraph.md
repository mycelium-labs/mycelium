# LangGraph integration

Mycelium is framework-agnostic — it works on **plain Python tools** and **message lists**. This guide covers the patterns LangGraph users hit most often, with links to real GitHub issues.

## Install

```bash
pip install mycelium-runtime   # Python 3.10+
mycelium init
```

Edit `mycelium.yaml`: list your tool function names under `tools:` and side-effect tools under `action_ledger.tools`.

## The three LangGraph problems we target

| Symptom | Real issue | Mycelium guard |
|---------|------------|----------------|
| Long tool call runs twice on cloud | [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417) | `ActionLedger` — claim `tool_call_id` before execution |
| Streamed output lost on cancel | [langgraph#5672](https://github.com/langchain-ai/langgraph/issues/5672) | `StateFlush` — persist partial state on cancel |
| Orphan / broken tool messages | [langgraph#7117](https://github.com/langchain-ai/langgraph/issues/7117) | `MessageValidator` — catch before LLM call |
| Invalid tool args dispatched | [langgraph#6431](https://github.com/langchain-ai/langgraph/issues/6431) | `@bounded` — block before tool runs |

## Pattern 1 — Stop duplicate tool execution (langgraph#7417)

LangGraph Cloud can redispatch a tool call while the original is still running. Mycelium records an **in-flight** claim keyed by `tool_call_id` so the second invocation does not re-execute.

**`mycelium.yaml` (minimal):**

```yaml
action_ledger:
  storage: file          # use redis for multi-worker / cloud — see below
  path: ./mycelium-ledger.json
  tools:
    - run_subagent
    - send_payment

tools:
  run_subagent:
    bounded:
      schema:
        task:
          type: string
          required: true
```

**Tool wrapper** — pass through `tool_call_id` from LangGraph / LangChain:

```python
from mycelium import ledger_sync

@ledger_sync()
def run_subagent(task: str, tool_call_id: str | None = None) -> dict:
    # tool_call_id is used automatically as the idempotency key
    return execute_long_task(task)
```

Or wire everything from YAML:

```python
import my_tools
from mycelium import load_config

config = load_config("mycelium.yaml")
tools = config.instrument(my_tools)  # applies ledger + bounded from YAML
```

**Multi-worker / LangGraph Cloud** — use Redis so all instances share the ledger:

```yaml
action_ledger:
  storage: redis
  url_env: MYCELIUM_REDIS_URL
  prefix: mycelium:action:
  in_flight_ttl: 3600
  tools:
    - run_subagent
```

```bash
export MYCELIUM_REDIS_URL=redis://localhost:6379/0
pip install 'mycelium-runtime[redis]'
```

## Pattern 2 — Validate messages before the model node

In your graph node that calls the LLM, run Mycelium guards on the message list:

```python
from mycelium import load_config

config = load_config("mycelium.yaml")

def call_model(state: dict) -> dict:
    messages = config.prepare_messages(state["messages"])
    response = model.invoke(messages)
    return {"messages": [response]}
```

`prepare_messages()` applies `MessageValidator`, `HistoryGuard`, and auto state recording when configured in YAML.

## Pattern 3 — Session per thread (cache isolation)

Wrap each LangGraph invocation in `config.run(thread_id)` so TTL caches do not leak across threads:

```python
config = load_config("mycelium.yaml")

def agent_node(state: dict) -> dict:
    thread_id = state.get("configurable", {}).get("thread_id", "default")
    with config.run(thread_id):
        messages = config.prepare_messages(state["messages"])
        ...
```

## Pattern 4 — State flush on cancel

If users cancel a run after streaming partial output, flush in-progress state:

```yaml
state_flush:
  storage: file
  path: ./mycelium-state.json
  flush_on:
    - cancel
    - disconnect
    - error
```

Use `config.run(thread_id)` — it wires `StateFlush` when `state_flush` is in YAML.

## Quick test (no LangGraph required)

```bash
pip install mycelium-runtime
mycelium demo
```

Or from this repo:

```bash
pip install -e "./sdk[dev]"
pytest proof/test_proof_af002.py::test_ledger_deduplicates_redispatched_tool_call_langgraph_7417 -v
```

## What Mycelium is not

- **Not a replacement for LangGraph checkpointing** — use both
- **Not Langfuse / tracing** — use Mycelium for prevention, Langfuse for post-hoc traces
- **Not LangChain-specific** — no LangGraph import required; wrap your own tool functions

## Links

- PyPI: https://pypi.org/project/mycelium-runtime/
- Proof fixture: `proof/fixtures/af002/langgraph-7417-duplicate-tool-execution.json`
