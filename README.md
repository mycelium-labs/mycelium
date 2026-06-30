# Mycelium

Runtime failure prevention for AI agents. Prevents predictable failures **before** they reach the LLM — not post-hoc observability.

## Shipped guards (v1.1)

| Problem | Guards |
|---------|--------|
| Stale or broken context | `@protect`, `Session`, `MessageValidator`, `HistoryGuard` |
| Bad or unauthorized tool calls | `@bounded`, `ToolRegistry`, `ToolRunner` |
| Duplicate side effects on retry | `ActionLedger`, `TaskLedger`, `StateFlush`, `AuditReceipt` |

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

**The bug:** LangGraph redispatches a tool call on retry while the original is still running → duplicate cost and side effects ([langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417)).

```bash
pip install mycelium-runtime
mycelium init                    # quickstart config for your side-effect tool
mycelium demo                    # see without / with Mycelium
```

```python
from mycelium import ledger_sync

@ledger_sync()
def subagent_task(task: str) -> dict:
    return run_slow_subagent(task)
```

Full YAML setup and more guards: [`sdk/README.md`](sdk/README.md) · LangGraph: [`docs/integrations/langgraph.md`](docs/integrations/langgraph.md)

## Repo layout

Public monorepo — source, tests, and issue-linked proofs live here. The PyPI wheel ships only `sdk/mycelium/`.

```
mycelium/                          ← git root (https://github.com/mycelium-labs/mycelium)
├── README.md                      ← you are here — product overview
├── CHANGELOG.md                   ← release notes
├── LICENSE                        ← MIT
├── .env.example                   ← optional local dev (HF corpus access)
├── .github/workflows/
│   ├── ci.yml                     ← test matrix 3.10–3.13 + proof + ruff
│   └── publish.yml                ← tag v* → PyPI (mycelium-runtime)
│
├── sdk/                           ← **publishable Python package**
│   ├── pyproject.toml             ← build config (hatchling)
│   ├── README.md                  ← PyPI long description + API reference
│   ├── mycelium/                  ← `import mycelium` (what ships on PyPI)
│   │   ├── templates/             ← bundled YAML templates (`mycelium init`)
│   │   └── …                      ← guards, config, storage backends
│   └── tests/                     ← unit tests (not in the wheel)
│
├── proof/                         ← issue-linked integration proofs
│   ├── README.md                  ← fixture catalog
│   ├── run_demo.py                ← human-readable demo
│   ├── test_proof*.py             ← parametrized proof tests
│   └── fixtures/                  ← real GitHub issue shapes (JSON)
│
├── docs/integrations/             ← framework guides (LangGraph, …)
│
└── planning/
    └── scope.md                   ← product scope, taxonomy, roadmap
```

### What `pip install` gets vs the repo

| Audience | Gets | Does not get |
|----------|------|--------------|
| `pip install mycelium-runtime` | `mycelium/*.py` + `mycelium/templates/*.yaml` | `proof/`, `planning/`, `tests/` |
| GitHub clone | Full tree above | Private HF failure corpus (optional; see `.env.example`) |

**Never commit:** `.env`, `sdk/.venv/`, `sdk/dist/`, `sdk/mycelium.yaml`, `__pycache__/`

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
- **LangGraph integration:** [`docs/integrations/langgraph.md`](docs/integrations/langgraph.md)
- Scope & roadmap: [`planning/scope.md`](planning/scope.md)
- Changelog: [`CHANGELOG.md`](CHANGELOG.md)
