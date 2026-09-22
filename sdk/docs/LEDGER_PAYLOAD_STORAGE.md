# Ledger payload storage

`ActionLedger` is both a transition guard and an evidence store. A durable
ledger entry can contain the arguments sent to a tool, its returned value, and
the error raised by a failed call. Treat the configured ledger backend as
operator-sensitive data, not as an opaque cache.

This page describes the behavior shipped today. It does not describe the
payload-policy design proposed in [issue #82](https://github.com/mycelium-labs/mycelium/issues/82).
See the [payload-controls proposal](LEDGER_PAYLOAD_POLICY_DESIGN.md) for design
decisions that still need review before implementation.

## What an action-ledger entry contains

Durable backends serialize `LedgerEntry.to_dict()`. The following fields are
part of that record:

| Field | Meaning and sensitivity |
| --- | --- |
| `request_id` | Physical row/key and dispatch identity. |
| `tool` | Configured tool name. |
| `args` | Positional invocation evidence; may contain the complete call payload. |
| `kwargs` | Keyword invocation evidence; may contain the complete call payload. |
| `status` | Legacy status (`in-flight`, `completed`, or `failed`). |
| `terminal_outcome` | Normalized terminal outcome, including ambiguous outcomes. |
| `fence` | Durable compare-and-set fencing token. |
| `result` | Tool return-value evidence; may contain provider or business data. |
| `error` | Formatted exception text from a failed call. |
| `started_at` | Claim/start timestamp. |
| `finished_at` | Terminal timestamp, when present. |
| `lease_until` | Worker lease expiry timestamp, when present. |
| `owner` | Worker/owner identifier, when present. |
| `idempotency_key` | Legacy durable key; defaults to `request_id`. |
| `receipt_ref` | Optional audit-receipt reference. |
| `side_effect_boundary` | Whether the external-effect boundary is `not_crossed`, `maybe_crossed`, or `crossed`. |
| `external_operation_ref` | Provider operation handle used for reconciliation. |
| `provider_idempotency_key` | Provider idempotency key, when configured or supplied. |
| `provider_key_first_attempt_at` | Timestamp of the first provider-key attempt. |
| `last_heartbeat_at` | Most recent worker heartbeat, when present. |
| `worker_dead_asserted_by` | Actor that asserted worker death, when present. |
| `worker_dead_asserted_at` | Worker-death assertion timestamp, when present. |
| `operator_resolution` | Manual resolution (`completed` or `not_executed`), when present. |
| `resolved_by` | Operator identity recorded for a manual resolution. |
| `resolution_reason` | Operator-supplied reason for a manual resolution. |
| `resolved_at` | Manual-resolution timestamp. |
| `released_from_outcome` | Outcome that preceded a manual release. |
| `decision_id` | Optional state-authority/decision identifier. |
| `state_ref` | Optional state-authority reference. |
| `decision` | Serialized policy-predicate verdicts and decision evidence, when recorded. |
| `effect_phase` | Unified effect-protocol phase (`INTENDED`, `ATTEMPTING`, `COMMITTED`, `ABORTED`, or `UNKNOWN`). |
| `effect_protocol_required` | Whether the unified effect protocol was required. |
| `effect_id` | Stable deduplication identity. |
| `request_id_aliases` | Host request IDs that resolved to the canonical effect row. |
| `schema_version` | Serialized entry-shape version. |
| `parent_request_id` | Optional handoff/causation parent request ID. |
| `handoff_id` | Optional handoff identifier. |

The decorator and configured wrapper paths build an evidence copy before
claiming a transition. An active `secret_args` policy, entity guard, or
destructive-confirm policy can sanitize that copy; with no active policy,
`args`, `kwargs`, and `result` may be stored as supplied. `error` is the
formatted exception text unless the active secret policy sanitizes it. Direct
calls to a storage class can write whatever a caller puts in a
`LedgerEntry`.

The JSON-backed backends use `default=str` while serializing. Values that are
not JSON-native can therefore be stored as their string representation rather
than round-tripping as the original Python type.

## Where the entry is stored

| Backend | Entry payload | Additional durable data | Retention behavior |
| --- | --- | --- | --- |
| `memory` | Python objects in the current process; nothing survives restart. | None. | No automatic retention; process-local entries can still be removed through the API. |
| `file` | A JSON object at `path`, keyed by `request_id`; each value is the full `to_dict()` payload. | `<path>.effect-index.json` stores `effect_id` → canonical `request_id` only. | No automatic expiry. Use an explicit prune window or reviewed `delete_transitions()` call. |
| `sqlite` | The configured SQLite file contains a table (default `mycelium_action_ledger`) with `request_id` and a JSON `payload`; the payload is the full `to_dict()` record. | A unique expression index is derived from `payload.effect_id`; indexes do not duplicate call/result data. | No automatic expiry. Use an explicit prune window or reviewed `delete_transitions()` call. |
| `redis` | A JSON value at `<prefix><request_id>`; the value is the full `to_dict()` payload. | A no-TTL tombstone keeps a copy for recovery; effect and sorted-set indexes contain identifiers/timestamps. | `in_flight_ttl` is a primary-key TTL, not data retention: the tombstone can rehydrate the payload. `retention_seconds` supplies a default prune window only. |
| `postgres` | A row in the configured table (default `mycelium_action_ledger`) with `request_id` and a JSONB `payload`; the payload is the full `to_dict()` record. | Unique effect and status/time indexes contain derived identifiers/timestamps. | `retention_seconds` supplies a default prune window only; it is not a background deletion job. |

The action ledger does not support `storage: shared`. Its configuration path
accepts `memory`, `file`, `sqlite`, `redis`, and `postgres`.
`storage: shared` belongs to separate stateful guard configurations, which use
the top-level `state_backend`; it is not an ActionLedger storage mode.

`memory` is still useful for tests and disposable single-process runs, but it
does not provide a durable safety boundary across workers or restarts.

## Redaction and exported evidence

There is currently no backend-independent field-selection, encryption, or
payload-retention setting for `LedgerEntry`. Until issue #82's policy is
implemented, operators should:

1. Enable the applicable evidence sanitizers before configuring a durable
   ledger. `secret_args` can reject or sanitize secret material, but it does
   not replace a review of the fields a tool returns.
2. Restrict access to ledger files, SQLite databases, Redis namespaces, and
   Postgres tables as production data stores.
3. Remember that an optional `audit_receipt` is a second representation. A
   receipt stores `inputs` (`args`/`kwargs`), `outputs`, and `error`, plus
   signing metadata. Deleting an action-ledger row does not delete its receipt.

The transitions CLI sanitizes values for `list`, `show`, `export`, and archive
output. That protects the rendered/exported representation; it does not
rewrite an existing backend row. A direct backend read therefore still needs
the same access controls as the application.

## Pruning safely

`ActionLedger.prune_transitions()` and `mycelium transitions prune` are
explicit operations. The default selection includes only `COMPLETED` and
`FAILED_BEFORE_EFFECT`; ambiguous, blocked, expired, and in-flight entries are
retained unless an operator explicitly selects those outcomes. Pruning is a
dry run unless `--execute` is supplied, and an archive can be written before
deletion:

```bash
mycelium transitions prune --sqlite ./mycelium-ledger.db \
  --older-than 30d --archive transitions.ndjson --execute
```

Use the corresponding `--file`, `--redis-url`, or `--postgres-dsn` selector
for another backend. For Redis and Postgres, `retention_seconds` is used only
when the API/CLI omits an explicit age; schedule and review the prune command
in the operator environment. It does not cause rows to disappear by itself.

Before deleting records, confirm that any required provider-reconciliation
evidence, audit receipts, and compliance archive have been retained. A direct
`delete_transitions()` call removes the selected ledger records (and the
backend's associated effect index/tombstone data) without creating an archive.
