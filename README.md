# Mycelium

Runtime failure prevention for AI agents. Prevents predictable failures **before** they reach the LLM — not post-hoc observability.

## Shipped failure modes (v1.0)

| ID | Mode | Guards |
|----|------|--------|
| AF-006 | Context corruption | `@protect`, `Session`, `MessageValidator`, `HistoryGuard` |
| AF-004 | Tool misuse | `@bounded`, `ToolRegistry`, `ToolRunner` |
| AF-002 | Observability black hole | `ActionLedger`, `TaskLedger`, `StateFlush`, `AuditReceipt` |

## AF-002: prevention, not an observability platform

**"Observability black hole"** names the *failure* — agents take consequential actions with no durable, trustworthy record. It does **not** mean Mycelium is a tracing or dashboard product.

| | Langfuse / Helicone / Opik | Mycelium AF-002 |
|--|--|--|
| **When** | After the agent runs | **Before / during** side effects |
| **What** | Traces, spans, dashboards | Idempotency keys, ledgers, state flush, signed receipts |
| **Goal** | See what happened | **Prevent** duplicate execution, lost state, unverifiable actions |
| **LLM calls** | Often required for analysis | **Zero** — deterministic guards only |

AF-002 guards are **runtime prevention**, not post-hoc monitoring:

- **ActionLedger / TaskLedger** — stop duplicate payments and retries from re-executing
- **StateFlush** — persist partial state on cancel so streamed output isn't lost
- **AuditReceipt** — tamper-evident proof an action happened (for compliance), not a trace UI

Use Mycelium **with** your existing observability stack if you want both. Mycelium does not replace it.

## Quick start

**Requires Python 3.10+** (3.11+ recommended).

```bash
pip install ./sdk
# or: pip install mycelium-sdk   # once published to PyPI
```

```python
from mycelium import load_config
import my_tools

config = load_config("mycelium.yaml")
tools = config.instrument(my_tools)

with config.run(thread_id):
    messages = config.prepare_messages(messages)
    ...
```

Copy `sdk/examples/mycelium.template.yaml` → `mycelium.yaml` and edit the global sections.

## Repo layout

| Path | What |
|------|------|
| [`sdk/`](sdk/) | Python package (`mycelium-sdk`) |
| [`proof/`](proof/) | Issue-linked proof fixtures + tests |
| [`planning/`](planning/) | Scope, taxonomy, roadmap |

## Proof

Each guard cites a real GitHub issue and reproduces the failure class:

```bash
cd sdk && pip install -e ".[dev]"
pytest tests/ -v
cd .. && PYTHONPATH=sdk pytest proof/ -v
python proof/run_demo.py
```

See [`proof/README.md`](proof/README.md).

## Docs

- SDK reference: [`sdk/README.md`](sdk/README.md)
- Scope & roadmap: [`planning/scope.md`](planning/scope.md)
- Changelog: [`CHANGELOG.md`](CHANGELOG.md)
