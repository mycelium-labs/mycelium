# Mycelium

Runtime failure prevention for AI agents. Prevents predictable failures **before** they reach the LLM — not post-hoc observability.

## Shipped failure modes (v1.0)

| ID | Mode | Guards |
|----|------|--------|
| AF-006 | Context corruption | `@protect`, `Session`, `MessageValidator`, `HistoryGuard` |
| AF-004 | Tool misuse | `@bounded`, `ToolRegistry`, `ToolRunner` |
| AF-002 | Observability black hole | `ActionLedger`, `TaskLedger`, `StateFlush`, `AuditReceipt` |

## Quick start

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
