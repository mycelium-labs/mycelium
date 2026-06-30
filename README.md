# Mycelium

Runtime failure prevention for AI agents. Prevents predictable failures **before** they reach the LLM — not post-hoc observability.

## Shipped failure modes (v1.1)

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
pip install mycelium-runtime
mycelium init                    # creates ./mycelium.yaml in your project
# or: mycelium init --minimal
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

Edit `mycelium.yaml` — rename tools/tasks to match your Python functions.

## Repo layout

This is a **monorepo**: the public PyPI package lives under `sdk/`; proof and planning stay outside the wheel.

```
mycelium/                          ← git root (private GitHub repo)
├── README.md                      ← you are here — product overview
├── CHANGELOG.md                   ← release notes
├── LICENSE                        ← MIT
├── .env.example                   ← HF token, signing key templates
├── .github/workflows/
│   ├── ci.yml                     ← test matrix 3.10–3.13 + proof + ruff
│   └── publish.yml                ← tag v* → PyPI (mycelium-runtime)
│
├── sdk/                           ← **publishable Python package**
│   ├── pyproject.toml             ← build config (hatchling)
│   ├── uv.lock                    ← uv lockfile (dev)
│   ├── README.md                  ← PyPI long description + API reference
│   ├── mycelium/                  ← `import mycelium` (what ships on PyPI)
│   │   ├── templates/             ← bundled YAML templates (`mycelium init`)
│   │   ├── protect.py …           ← AF-006 guards
│   │   ├── tool_*.py              ← AF-004 guards
│   │   ├── *_ledger.py …          ← AF-002 guards
│   │   ├── config.py              ← YAML loader
│   │   └── storage/               ← file / redis / postgres backends
│   ├── tests/                     ← unit tests (not published)
│   └── examples/README.md         ← points to `mycelium init` (not published)
│
├── proof/                         ← issue-linked integration proofs (not published)
│   ├── README.md                  ← fixture catalog
│   ├── run_demo.py                ← human-readable demo
│   ├── test_proof*.py             ← parametrized proof tests
│   └── fixtures/                  ← real GitHub issue shapes (JSON)
│
└── planning/
    └── scope.md                   ← product scope, taxonomy, roadmap
```

### What users install vs what stays in the repo

| Audience | Gets | Does not get |
|----------|------|--------------|
| `pip install mycelium-runtime` | `mycelium/*.py` + `mycelium/templates/*.yaml` | `proof/`, `planning/`, `tests/` |
| Repo collaborators | Full tree above | — |

**Local dev junk** (never commit): `sdk/.venv/`, `sdk/dist/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`

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
