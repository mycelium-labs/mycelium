# Mycelium

[![PyPI version](https://img.shields.io/pypi/v/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)
[![Python](https://img.shields.io/pypi/pyversions/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)

**Runtime guards for AI agents.** Prevents predictable failures before they reach the LLM. Not tracing or dashboards.

## Who it's for

Developers running **agents with side-effect tools** in production (payments, emails, API writes, long subagent calls) on **LangGraph, CrewAI, or a plain Python loop**.

Python 3.10+. Framework-agnostic.

## What it does

- **Duplicate side effects on retry:** same `tool_call_id` won't charge, send, or execute twice
- **Stale or broken context:** fresh tool data, valid message transcripts
- **Bad tool calls:** block invalid inputs and out-of-scope tools before they run

Not Langfuse. Use both if you want traces and guards.

## Use it

```bash
pip install mycelium-runtime
mycelium demo          # see the bug and the fix
mycelium init          # scaffold mycelium.yaml
```

```python
from mycelium import ledger_sync

@ledger_sync()
def subagent_task(task: str) -> dict:
    return run_slow_subagent(task)

subagent_task(task="analyze_market", tool_call_id=call["id"])
```

Pass `tool_call_id` from your framework. Redispatch returns the cached result. No second side effect.

Multi-worker / cloud: `pip install 'mycelium-runtime[redis]'`. See the [handbook](https://mycelium-labs.github.io/mycelium/).

## Docs

- **Handbook:** https://mycelium-labs.github.io/mycelium/
- **Full API reference:** [sdk/README.md](sdk/README.md)
- **PyPI:** https://pypi.org/project/mycelium-runtime/

## License

MIT. See [LICENSE](LICENSE).
