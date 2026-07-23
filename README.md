# Mycelium

[![PyPI version](https://img.shields.io/pypi/v/mycelium-runtime.svg?cacheSeconds=60&release=1.13.4)](https://pypi.org/project/mycelium-runtime/)
[![Python](https://img.shields.io/pypi/pyversions/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)
[![Downloads](https://static.pepy.tech/badge/mycelium-runtime)](https://pepy.tech/project/mycelium-runtime)

**Runtime guards and zero-touch YAML auto-instrumentation for AI agents.**

Stops duplicate side effects on retry/redispatch, blocks bad tool args and out-of-scope calls, and keeps tool data fresh. Not recovery after. Not tracing or dashboards.

*Early but API-stable (**v1.13.4**): breaking changes only at major versions. More guards planned.*

## Who it's for

Developers running **agents with side-effect tools** in production (payments, emails, API writes, long subagent calls) on **LangGraph, CrewAI, or a plain Python loop**.

Python 3.10+. Framework-agnostic.

## What it does (v1.13.x)

These aren't reasoning failures. They're runtime failures. Mycelium sits between your agent loop and your tools (after the LLM returns `tool_calls`):

**Core** (`mycelium init` / `mycelium run`):

- **Duplicate side effects on retry:** classify tools (`read` vs `keyed_mutate` vs `non_idempotent_mutate`, etc.), hash a durable **transition key**, resolve duplicates by **terminal state** — not blind re-execute. **Do not redispatch unless the previous transition is proven terminal or safely recoverable.** This is a **transition envelope** (class + lease + terminal + hard-block / reconcile), not only an idempotency key plus a cached result.
  - **Read tools:** poll in-flight, reclaim expired leases, **soft-block** ambiguous `UNKNOWN` (safe retry by default)
  - **Mutating tools:** hard-block ambiguity; **reconcile** via `external_operation_ref` when a provider lookup can prove run-or-not (`COMPLETED` / `NOT_EXECUTED` / still blocked)
  - **Stale lease (`EXPIRED`):** strict classes reclaim only when reconcile proves `NOT_EXECUTED` (fail-closed without a ref)
  - **LangGraph Cloud:** long tools may be redispatched around **~180s** (`BG_JOB_HEARTBEAT` sweep); Mycelium’s lease/poll/hard-block guards that window ([langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417))
- **Transition envelope fields** (priority order): `side_effect_class` → `spendability` → `side_effect_boundary` → `terminal_outcome` → `external_operation_ref` → `retry_permission` — payment/write needs the heavier set; without it, redispatch is an unsupported second transition, not a retry

**Opt-in** (configure or call explicitly):

- **Stale or broken context:** TTL-fresh tool data (`@protect`); optional message/history validation before the next LLM turn
- **Bad tool calls:** block invalid inputs and out-of-scope tools before they run (`@bounded` / registry)

Not Langfuse. Use both if you want traces and guards. Full resolution rules: [sdk/README.md](sdk/README.md#resolution-gates). Envelope field stack: [sdk/README.md](sdk/README.md#transition-envelope-fields).

## Use it

```bash
pip install mycelium-runtime
pip install 'mycelium-runtime[langgraph]'  # automatic LangGraph runtime IDs
pip install 'mycelium-runtime[redis]'      # multi-worker / cloud ledger
pip install 'mycelium-runtime[postgres]'   # Postgres ledger backend
mycelium demo              # see the bug and the fix
mycelium init              # on-ramp: transition + one ledgered tool → mycelium.yaml
mycelium init --full       # reference: all guards (fill TODOs; not the default)
mycelium init --minimal    # smaller multi-guard scaffold
```

`mycelium init` is the real start path (duplicate-tool fix). Use `--full` when you want every section documented in one file.

```yaml
# after: mycelium init
integrations:
  langgraph:
    enabled: true

transition:
  agent_id: my-agent
  policy_version: "2026.07.1"

action_ledger:
  storage: file
  path: ./mycelium-ledger.json
  tools: [my_side_effect_tool]

tools:
  my_side_effect_tool:
    callable: my_app.tools:my_side_effect_tool
    side_effect_class: non_idempotent_mutate
```

Launch your existing Python application without adding decorators:

```bash
mycelium run --config mycelium.yaml -- python -m my_app
```

`mycelium run` validates and wraps every configured callable before the
application starts. It preserves the child process's arguments, working
directory, signals, and exit code. The command accepts the current Python
interpreter only.

Explicit instrumentation remains supported when you prefer code-level control:

```python
from mycelium import load_config

config = load_config("mycelium.yaml")

@config.apply
def my_side_effect_tool(...) -> dict:
    ...
```

Without YAML, use the ledger decorators directly (`@ledger` / `@ledger_sync` for tools;
`@task_ledger` / `@task_ledger_sync` for coarser task-level idempotency). Same transition
envelope and gates — see [sdk/README.md](sdk/README.md#what-ledger--ledger_sync-do)
and [task-level idempotency](sdk/README.md#quickstart-task-level-idempotency).

Do not combine standalone guard decorators with command mode on the same
function. Fully configured `@config.apply` wrappers are detected and skipped.
Keep callable modules import-safe: registrations performed inside a target
module while that module is still importing cannot be retroactively replaced.

With the optional LangGraph integration, `ToolNode` / `create_agent` injects
`ToolRuntime`; Mycelium automatically maps its `tool_call_id`, thread, run, and
node into transition identity. Explicit IDs still override captured values.
Custom tool executors can continue passing `tool_call_id` manually. Redispatch
resolves the existing transition: read tools poll/soft-block; mutating tools
hard-block or reconcile against the provider when you record
`external_operation_ref`.

Multi-worker / cloud ledgers: `pip install 'mycelium-runtime[redis]'` or
`'mycelium-runtime[postgres]'`. See the [handbook](https://mycelium-labs.github.io/mycelium/).

## Docs

- **Handbook:** https://mycelium-labs.github.io/mycelium/
- **Full API reference:** [sdk/README.md](sdk/README.md)
- **PyPI:** https://pypi.org/project/mycelium-runtime/

## License

MIT. See [LICENSE](LICENSE).
