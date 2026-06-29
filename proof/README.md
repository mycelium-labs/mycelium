# Mycelium proof artifacts (AF-006 + AF-004 + AF-002 in research)

Legitimate proof means: **cite a real issue → reproduce its failure class → show Mycelium catches or fixes it.**

This is not marketing copy or fake chat text. Each fixture links to a public GitHub issue and uses message/tool shapes that match the reported bug mechanism.

## AF-006 — context integrity (`fixtures/`)

| Fixture | Real issue | Component | Proof |
|---|---|---|---|
| `langchain-36984-fc-call-duplicate.json` | [langchain#36984](https://github.com/langchain-ai/langchain/issues/36984) | MessageValidator | `fc_*` + `call_*` duplicates → validate raises → repair fixes |
| `langchain-31511-nonzero-index.json` | [langchain#31511](https://github.com/langchain-ai/langchain/issues/31511) | MessageValidator | tool_calls start at index 1 → repair renumbers to 0 |
| `langgraph-7117-orphan-tool-result.json` | [langgraph#7117](https://github.com/langchain-ai/langgraph/issues/7117) | MessageValidator | orphan tool result → validate raises (unfixable) |
| `stale-tool-result-ttl.json` | [cline#7462](https://github.com/cline/cline/issues/7462) | `@protect` | DB updates after cache → TTL expiry refetches fresh data |
| `history-silent-drop.json` | [cline#7462](https://github.com/cline/cline/issues/7462) | HistoryGuard | framework trims history → `check_for_drops()` raises |

## AF-004 — tool boundary (`fixtures/af004/`)

| Fixture | Real issue | Component | Proof |
|---|---|---|---|
| `cline-10737-invalid-tool-args.json` | [cline#10737](https://github.com/cline/cline/issues/10737) | `@bounded` | missing required field → `ToolBoundaryError` before tool runs |
| `langgraph-6431-invalid-input.json` | [langgraph#6431](https://github.com/langchain-ai/langgraph/issues/6431) | `@bounded` | null/invalid arg → input validation blocks dispatch |
| `cline-8273-path-scope.json` | [cline#8273](https://github.com/cline/cline/issues/8273) | `@bounded` | path outside workspace → `scope_path` violation |
| `langchain-34669-output-shape.json` | [langchain#34669](https://github.com/langchain-ai/langchain/issues/34669) | `@bounded` | MCP returns list not record → output validation fails |
| `langchain-35320-allowlist.json` | [langchain#35320](https://github.com/langchain-ai/langchain/issues/35320) | `ToolRegistry` | tool not in allowlist → `not_in_allowlist` |
| `cline-8779-llm-retry-recovery.json` | [cline#8779](https://github.com/cline/cline/issues/8779) | `ToolRunner` | bad args → tool error → LLM retry with corrected kwargs |

## AF-002 — observability black hole (`fixtures/af002/`)

**Note:** This names the *failure class*, not a tracing product. Proofs show prevention guards (ledger, flush, receipt) — not dashboard-style observability.

Shipped guards: `ActionLedger`, `TaskLedger`, `StateFlush`, `AuditReceipt`.

Next failure mode. Fixtures capture the real issues that v2 prevents.

| Fixture | Real issue | Component | Pattern |
|---|---|---|---|
| `langgraph-7417-duplicate-tool-execution.json` | [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417) | `ActionLedger` | long tool call redispatched because no durable in-flight record exists |
| `crewai-5802-retry-idempotency.json` | [crewAI#5802](https://github.com/crewAIInc/crewAI/issues/5802) | `ActionLedger` | task retry re-executes already-completed side-effect tools |
| `langgraph-5672-cancelled-state-loss.json` | [langgraph#5672](https://github.com/langchain-ai/langgraph/issues/5672) | `StateFlush` | streamed state lost on cancel because it was never checkpointed |
| `autogen-7353-missing-audit-receipt.json` | [autogen#7353](https://github.com/microsoft/autogen/issues/7353) | `AuditReceipt` | traces/logs exist but are not auditor-verifiable |

## Run the demo

```bash
cd sdk && python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cd ..
python proof/run_demo.py
```

## Run the proof tests

```bash
pytest proof/test_proof.py proof/test_proof_af004.py proof/test_proof_af002.py proof/test_proof_af002_task.py -v
```

## How to add a new legitimate case

1. **Find a real issue** in your tagged corpus (`incidents/`) or GitHub with a clear mechanism in the body/comments.
2. **Extract the failure class** — not the whole conversation, just the structural bug (orphan tool result, stale cache, etc.).
3. **Add a fixture JSON** with:
   - `source_url`, `source_title`, `pattern`
   - `messages` or scenario data that reproduces the class (AF-006), or `schema_fields` / `bad_kwargs` / `good_kwargs` (AF-004)
   - `violation` expected from Mycelium
4. **Add a parametrized test** in `test_proof.py` (AF-006) or `test_proof_af004.py` (AF-004) that proves catch/fix.
5. **Optional:** add a line to `run_demo.py` for human-readable output.

## What makes it legitimate vs fake

| Legitimate | Fake |
|---|---|
| Links to real GitHub issue | Made-up "user said hello" chats |
| Message shape matches reported bug | Generic placeholder content |
| Observable pass/fail (raise or repair) | Hand-wavy "it works" text |
| Same violation every run | Subjective LLM judgment |

## Next step (optional)

The archive branch vendored [agent-test-AF006](https://github.com/mycelium-labs/agent-test-AF006) as a submodule with broader property tests. You can re-add that for CI once this repo is ready.
