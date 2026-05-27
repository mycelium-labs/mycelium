# examples

Runnable samples for the **public** Mycelium API (`@protect`, `Session`, guards).

**Start here:** [docs/af006-integration.md](../docs/af006-integration.md) — integration recipe (prevent vs flag vs repair).

| File | Description |
|------|-------------|
| `integration_recipe.py` | Minimal agent loop: `Session` + `@protect` + `MessageValidator.repair()` |
| `langgraph_integration_example.py` | LangGraph-shaped flow (no adapter; tools are plain `@protect` callables) |
| `crewai_integration_example.py` | Sync tools with `protect_sync` (Crew-shaped `_run` pattern) |
| `benchmark_protect_decorator.py` | Throughput benchmark for `@protect` |

Named-issue reproducers and framework e2e live in **[agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006)**.

```bash
# From repo root
pip install ./sdk
python examples/integration_recipe.py
```
