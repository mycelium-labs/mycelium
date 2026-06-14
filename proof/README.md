# AF-006 proof artifact

Legitimate proof means: **cite a real issue → reproduce its failure class → show Mycelium catches or fixes it.**

This is not marketing copy or fake chat text. Each fixture links to a public GitHub issue and uses message/tool shapes that match the reported bug mechanism.

## What's in here

| Fixture | Real issue | Component | Proof |
|---|---|---|---|
| `langchain-36984-fc-call-duplicate.json` | [langchain#36984](https://github.com/langchain-ai/langchain/issues/36984) | MessageValidator | `fc_*` + `call_*` duplicates → validate raises → repair fixes |
| `langchain-31511-nonzero-index.json` | [langchain#31511](https://github.com/langchain-ai/langchain/issues/31511) | MessageValidator | tool_calls start at index 1 → repair renumbers to 0 |
| `langgraph-7117-orphan-tool-result.json` | [langgraph#7117](https://github.com/langchain-ai/langgraph/issues/7117) | MessageValidator | orphan tool result → validate raises (unfixable) |
| `stale-tool-result-ttl.json` | [cline#7462](https://github.com/cline/cline/issues/7462) | `@protect` | DB updates after cache → TTL expiry refetches fresh data |
| `history-silent-drop.json` | [cline#7462](https://github.com/cline/cline/issues/7462) | HistoryGuard | framework trims history → `check_for_drops()` raises |

## Run the demo

```bash
cd sdk && python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cd ..
python proof/run_demo.py
```

## Run the proof tests

```bash
pytest proof/test_proof.py -v
```

## How to add a new legitimate case

1. **Find a real issue** in your tagged corpus (`incidents/`) or GitHub with a clear mechanism in the body/comments.
2. **Extract the failure class** — not the whole conversation, just the structural bug (orphan tool result, stale cache, etc.).
3. **Add a fixture JSON** with:
   - `source_url`, `source_title`, `pattern`
   - `messages` or scenario data that reproduces the class
   - `violation` expected from Mycelium
4. **Add a parametrized test** in `test_proof.py` that proves catch/fix.
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
