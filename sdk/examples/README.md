# Bundled examples

Runnable examples for the Mycelium Python runtime. A curated, credential-free
subset is smoke-tested in CI so an example cannot silently drift from the public
API or the configuration schema.

Run the smoke suite from `sdk/`:

```bash
pytest tests/test_examples_smoke.py -rs
```

`-rs` prints every skip with its reason. The manifest lives in
[`tests/test_examples_smoke.py`](../tests/test_examples_smoke.py).

## What the smoke suite checks

| Mode | What runs | Fails when |
|------|-----------|------------|
| **script** | `python <example>` in a subprocess | Exit code is non-zero, or its behavioural marker is missing from stdout |
| **config** | Schema validation and `load_config()` for the YAML | A field no longer matches the typed configuration model |
| **stubbed** | The example is imported and driven with stub provider and model callables | The example's use of the public API breaks |
| **existing** | Nothing extra; a dedicated test already runs the example | That test disappears or stops referencing the example |

Scripts run without provider credentials or `MYCELIUM_*` backend settings, and
any non-loopback socket connection raises, so ordinary CI never makes an
external call. Their stdout is pinned to `cp1252`, the default Windows console
encoding, so an example that prints a character Windows cannot display fails
here.

A file added under `examples/` that is not in the manifest fails the suite. Add
it to the manifest with a mode, or say why it is skipped.

## Curated list

| Category | Example | Mode | Needs |
|----------|---------|------|-------|
| LangGraph | `langgraph_email_onboarding/run.py` | script | nothing (local sandbox provider) |
| LangGraph | `langgraph_email_onboarding/mycelium.yaml` | config | nothing |
| LangGraph | `langgraph_redis_crash/run.py` | existing ([`test_example_langgraph_redis_crash.py`](../tests/test_example_langgraph_redis_crash.py)) | Redis and `langgraph` |
| LangGraph | `langgraph_redis_crash/mycelium.example.yaml` | config | nothing |
| Failure gates | `failure_cases/run_all.py` | existing ([`test_failure_cases.py`](../tests/test_failure_cases.py)) | nothing |
| Failure gates | `failure_cases/01_return_completed.py` | existing | nothing |
| Failure gates | `failure_cases/02_poll_in_flight.py` | existing | nothing |
| Failure gates | `failure_cases/03_hard_block_unknown.py` | existing | nothing |
| Failure gates | `failure_cases/04_repair_incomplete.py` | existing | nothing |
| Failure gates | `failure_cases/05_reconcile_completed.py` | existing | nothing |
| Python guards | `loop_guard_db_search.py` | script | nothing |
| Python guards | `scope_guard_allowlist.py` | script | nothing |
| Python guards | `completion_contract_checklist.py` | script | nothing |
| Webhooks | `webhooks/stripe_handler.py` | script | nothing (fakes) |
| Webhooks | `webhooks/github_handler.py` | script | nothing (fakes) |
| Webhooks | `webhooks/twilio_handler.py` | script | nothing (fakes) |
| Agent application | `groq_support_agent.py` | stubbed | nothing (stub model and provider) |
| Configuration | `mycelium.generated.example.yaml` | config | nothing |

## Intentional skips

| Skipped | Reason |
|---------|--------|
| `groq_support_agent.py`: `GroqDecisionModel` live call | requires GROQ_API_KEY and network access to api.groq.com |

The agent itself is still smoke-tested with a stub decision model, so only the
live model call is skipped.

`langgraph_redis_crash` needs a reachable Redis. Locally its test skips with a
visible reason when Redis is missing. In CI, where `MYCELIUM_CI_REQUIRE_REDIS=1`,
a missing Redis fails the test instead of skipping it.

## Not bundled here

- **CrewAI:** there is no example under `examples/`.
- **TypeScript and Go:** the sidecar clients are covered by the cross-language
  [conformance suite](../../conformance/README.md), not by this directory.
