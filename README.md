# Mycelium

[![PyPI version](https://img.shields.io/pypi/v/mycelium-runtime.svg?cacheSeconds=60)](https://pypi.org/project/mycelium-runtime/)
[![Python](https://img.shields.io/pypi/pyversions/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)

**Runtime guards for AI agents.**

Prevents predictable failures *before* they reach the LLM. Not recovery after. Not tracing or dashboards.

*Experimental early release (**v1.3.3**). More guards planned.*

## Who it's for

Developers running **agents with side-effect tools** in production (payments, emails, API writes, long subagent calls) on **LangGraph, CrewAI, or a plain Python loop**.

Python 3.10+. Framework-agnostic.

## What it does (v1.3.x)

These aren't reasoning failures. They're runtime failures. Mycelium sits between your agent loop and your tools:

- **Duplicate side effects on retry:** classify tools (`read` vs `keyed_mutate` vs `non_idempotent_mutate`, etc.), hash a durable transition key, resolve duplicates by terminal state — poll reads, hard-block ambiguous writes
- **Stale or broken context:** fresh tool data, valid message transcripts
- **Bad tool calls:** block invalid inputs and out-of-scope tools before they run

Not Langfuse. Use both if you want traces and guards.

## Use it

```bash
pip install mycelium-runtime
mycelium demo          # see the bug and the fix
mycelium init          # scaffold mycelium.yaml
```

```yaml
# mycelium.yaml
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"

tools:
  send_payment:
    side_effect_class: keyed_mutate
```

```python
from mycelium import load_config

config = load_config("mycelium.yaml")

@config.apply
def send_payment(amount: float, recipient: str) -> dict:
    return gateway.charge(amount, recipient)

send_payment(amount=100.0, recipient="acct_123", tool_call_id=call["id"])
```

Pass `tool_call_id` from your framework. Redispatch resolves the existing transition — read tools poll and return; mutating tools won't execute twice unsafely.

Multi-worker / cloud: `pip install 'mycelium-runtime[redis]'`. See the [handbook](https://mycelium-labs.github.io/mycelium/).

## Docs

- **Handbook:** https://mycelium-labs.github.io/mycelium/
- **Full API reference:** [sdk/README.md](sdk/README.md)
- **PyPI:** https://pypi.org/project/mycelium-runtime/

## License

MIT. See [LICENSE](LICENSE).
