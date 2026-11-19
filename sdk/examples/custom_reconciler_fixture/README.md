# Custom reconciler conformance fixture

A minimal, provider-neutral reconciler that passes the conformance kit. Copy
this file when you need a starting point that is not the Gmail implementation.
It talks to a fully local fake provider, so it needs no credentials and makes
no network calls.

It is a sample integration. It is not another provider SDK and it does not
replace the conformance runner.

## Run it

From `sdk/`:

```bash
python examples/custom_reconciler_fixture/run.py
```

Expected last line:

```text
conformance ok: adapter=local-note-example cases=8 verified=True tamper_refused=True synthetic_adapter_evidence=True
```

## What it demonstrates

| Case | What the fake fixture does |
| --- | --- |
| `exactly_one_match` | One provider match reconciles as `COMPLETED`. |
| `zero_matches_fail_closed` | No matches stay `UNKNOWN`; an empty result may be indexing lag. |
| `provider_indexing_lag` | A later read can still resolve the same entry. |
| `duplicate_matches_fail_closed` | Two matches stay `UNKNOWN`; neither is chosen. |
| `ambiguous_provider_responses` | Provider errors and malformed payloads never prove non-execution. |
| `malformed_handles_rejected_locally` | Bad handles are refused before any provider call. |
| `no_false_not_executed` | Uncertain evidence is never reported as `NOT_EXECUTED`. |
| `read_only_no_forbidden_writes` | The reconciler never writes to the provider. |

The report is signed with a throwaway local key and verified in-process. It is
synthetic adapter evidence: it shows the reconciler fails closed for ambiguous
evidence. It does not prove live provider permissions, consistency, or
exactly-once execution, and it is not permission to run against production.

## Adapting it

1. Replace `_ScriptedNoteStore` with your provider's read-only client.
2. Keep `reconcile()` returning `UNKNOWN` whenever the evidence is not
   definitive. `NOT_EXECUTED` is only for authoritative non-execution.
3. Update `HANDLE_PATTERN`, `valid_handle`, and `malformed_handles` for your
   provider's id rules.
4. Bind `source_bytes()` to your reconciler, not to the example harness, so a
   report binds to the code you actually ship.

The required case set, report signature semantics, and runtime reconciliation
behaviour are unchanged by this example.
