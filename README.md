# Mycelium

[![PyPI version](https://img.shields.io/pypi/v/mycelium-runtime.svg?cacheSeconds=60)](https://pypi.org/project/mycelium-runtime/)
[![Python](https://img.shields.io/pypi/pyversions/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)
[![Downloads](https://static.pepy.tech/badge/mycelium-runtime)](https://pepy.tech/project/mycelium-runtime)

**Runtime guards for AI agents.**

Prevents predictable failures *before* they reach the LLM. Not recovery after. Not tracing or dashboards.

*Early but API-stable (**v1.9.2**): breaking changes only at major versions. More guards planned.*

## Who it's for

Developers running **agents with side-effect tools** in production (payments, emails, API writes, long subagent calls) on **LangGraph, CrewAI, or a plain Python loop**.

Python 3.10+. Framework-agnostic.

## What it does (v1.9.x)

These aren't reasoning failures. They're runtime failures. Mycelium sits between your agent loop and your tools:

- **Duplicate side effects on retry:** classify tools (`read` vs `keyed_mutate` vs `non_idempotent_mutate`, etc.), hash a durable **transition key**, resolve duplicates by **terminal state** — not blind re-execute
  - **Read tools:** poll in-flight, reclaim expired leases, **soft-block** ambiguous `UNKNOWN` (safe retry by default)
  - **Mutating tools:** hard-block ambiguity; **reconcile** via `external_operation_ref` when a provider lookup can prove run-or-not (`COMPLETED` / `NOT_EXECUTED` / still blocked)
  - **Stale lease (`EXPIRED`):** strict classes reclaim only when reconcile proves `NOT_EXECUTED` (fail-closed without a ref)
- **Stale or broken context:** fresh tool data, valid message transcripts
- **Bad tool calls:** block invalid inputs and out-of-scope tools before they run

Not Langfuse. Use both if you want traces and guards. Full resolution rules: [sdk/README.md](sdk/README.md#resolution-gates).

## Use it

```bash
pip install mycelium-runtime
mycelium demo              # see the bug and the fix
mycelium init              # on-ramp: transition + one ledgered tool → mycelium.yaml
mycelium init --full       # reference: all guards (fill TODOs; not the default)
mycelium init --minimal    # smaller multi-guard scaffold
```

`mycelium init` is the real start path (duplicate-tool fix). Use `--full` when you want every section documented in one file.

```yaml
# after: mycelium init
transition:
  agent_id: my-agent
  policy_version: "2026.07.1"

action_ledger:
  storage: file
  path: ./mycelium-ledger.json
  tools: [my_side_effect_tool]

tools:
  my_side_effect_tool:
    side_effect_class: non_idempotent_mutate
```

```python
from mycelium import load_config

config = load_config("mycelium.yaml")

@config.apply
def my_side_effect_tool(...) -> dict:
    ...

my_side_effect_tool(..., tool_call_id=call["id"])
```

Pass `tool_call_id` from your framework. Redispatch resolves the existing transition: read tools poll/soft-block; mutating tools hard-block or reconcile against the provider when you record `external_operation_ref`.

Multi-worker / cloud: `pip install 'mycelium-runtime[redis]'`. See the [handbook](https://mycelium-labs.github.io/mycelium/).

## Docs

- **Handbook:** https://mycelium-labs.github.io/mycelium/
- **Full API reference:** [sdk/README.md](sdk/README.md)
- **PyPI:** https://pypi.org/project/mycelium-runtime/

## License

MIT. See [LICENSE](LICENSE).
