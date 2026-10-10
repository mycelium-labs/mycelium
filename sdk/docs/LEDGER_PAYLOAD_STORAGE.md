# Ledger payload storage

`ActionLedger` is both a transition guard and an evidence store. A durable
ledger entry can contain the arguments sent to a tool, its returned value, and
the error raised by a failed call. Treat the configured ledger backend as
operator-sensitive data, not as an opaque cache.

The optional `action_ledger.payload_policy` controls argument and result evidence
for action-ledger wrappers. The [design note](LEDGER_PAYLOAD_POLICY_DESIGN.md)
records the remaining scope and migration considerations.

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
| `args_digest`, `args_alias_digest` | Stable argument-conflict fingerprints when argument evidence is omitted or redacted. Hashes can reveal low-entropy inputs by guessing. |
| `result_retained` | Whether a completed result can be replayed; distinguishes omission from a legitimate `None` return. |
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
| `tenant_id` | Optional trusted tenant identifier. |
| `policy_version` | Optional transition policy version. |
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

## Payload policy

The default retains arguments and results. For a configured wrapper, use:

```yaml
action_ledger:
  payload_policy:
    store_args: false
    store_result: true
    redact_fields: [authorization, api_key, message_body]
```

`store_args: false` leaves `args` empty and keeps only routing and scope keys
in `kwargs` (`request_id`, `tool_call_id`, `thread_id`, `run_id`, `node`,
`state_ref`, `decision_id`, `parent_request_id`, `handoff_id`, and configured
`scope_from` sources). It persists argument fingerprints for conflict detection;
effect identity is still computed from the original call before omission.
`redact_fields` replaces matching dictionary keys at any depth, including
dictionaries within lists, with `[REDACTED]`. It does not address positional
arguments by name; use `store_args: false` for those. Redacting a result marks
it unavailable for replay, even if other result fields remain. A legitimate
stored `None` remains replayable.

With `store_result: false`, the first caller receives the live result, while
the stored row contains `result: null` and `result_retained: false`. A later
duplicate raises `LedgerHardBlockError` after confirming completion; it does
not execute the tool again or return a misleading `None` or redacted object.
The host must retrieve the return value from provider or operator evidence if
needed. This policy also applies to operator-completed results and to receipts
emitted from the resulting ledger entry. It does not cover the separate task
ledger, directly constructed `LedgerEntry` writes to a storage backend, or
other evidence fields such as `error`, `decision`, provider references, and
operator reasons. Protect those stores and fields separately.

Payload-state fields use ledger entry schema 3. Older runtimes reject new rows;
upgrade all workers before writing with this version, and do not run mixed-version
writers. Existing schema 1/2 rows remain readable. `mycelium migrate`
can plan and apply an explicit upgrade of existing rows to schema 3; it does
not delete historical payloads.

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

There is no built-in encryption hook or automatic payload-only expiry.
Operators should:

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

## Coordinated audit-receipt cleanup

Transition pruning does **not** delete audit receipts. `FileAuditReceiptStorage`
and `AtomicAuditReceiptStorage` expose append/list operations, with no receipt
prune API or CLI. Receipts can retain signed inputs, outputs, and error text
after the transition is gone. Cleanup is a separate, operator-controlled
maintenance operation; there is no automatic cascade.

### Review and select before deleting

1. Stop the affected writers, including other workers using the same receipt
   store, and pause exporters that read it. Keep them stopped through cleanup.
2. Review the transition prune dry run and export the selected transitions
   before execution. Record their `receipt_ref` values and the receipt IDs
   approved for removal. A request can have multiple attempt receipts; inspect
   the receipt store's `request_id`, `action`, `action_kind`, and `agent_id` as
   well as the ledger reference. Request IDs alone are not a global join key.
3. Apply the receipt retention policy and any legal hold independently. Do not
   infer permission to delete from an old timestamp or a missing transition.
   Retain evidence needed for reconciliation and incident review.
4. Securely archive the selected original receipt records if policy requires
   it, then execute the reviewed transition prune and receipt cleanup. An
   archive is another copy of the payload; give it its own access and retention
   policy. This procedure is not a transaction across the two stores.
5. Verify the exact selected IDs are absent, retained receipts are unchanged,
   and ledger receipt references that remain are accounted for. Record the
   operation and counts before resuming writers and exporters.

### File receipts

With `audit_receipt: {storage: file, path: ...}`, the configured
`audit_receipt.path` is a newline-delimited JSON file, one complete signed
receipt per line. This is separate from the action-ledger JSON file and its
effect index. While writers are stopped, parse that file and write a sibling
temporary file containing only records whose `receipt_id` is **not** in the
reviewed deletion set. Preserve each retained record, including its signature;
do not redact or edit a signed payload in place.

Validate the temporary file and removal count, preserve restrictive file
permissions, flush it to disk, and replace the original on the same filesystem.
Do not truncate an actively written receipt file or delete the ledger's effect
index as a substitute. Include old rotated copies and backups in the reviewed
retention plan; replacing the active file does not erase those copies.

### Shared / atomic receipts

Configured Redis/Postgres receipts and `storage: shared` use a receipt namespace
`<base>:audit_receipt`. For direct Redis/Postgres configuration, `<base>` is
`audit_receipt.namespace` (default `mycelium`). For `storage: shared`, or omitted
receipt storage with a top-level `state_backend`, it is `state_backend.namespace`
(default `mycelium`). A directly constructed `AtomicAuditReceiptStorage` instead
defaults to the namespace `audit_receipt`; use the actual constructor value.

| Atomic backend | Receipt location | Scoped removal |
| --- | --- | --- |
| File | `state_backend.path`, a JSON object with records carrying `namespace`, `key`, `version`, and `value` | Remove only records in the receipt namespace with the approved receipt ID as `key`, through the backend API. Other guard state may share this file. |
| Redis | `<prefix><URL-encoded namespace>:<URL-encoded receipt_id>`; default prefix `mycelium:state:` | Remove only the selected receipt keys. Do not flush the database or delete all keys under the state prefix. |
| Postgres | Configured state table (default `mycelium_state`), with `namespace`, `state_key`, `version`, and JSONB `payload` | Delete only the selected `(namespace, state_key)` pairs. Do not drop the table or remove the other state namespaces. |
| Memory | Process-local atomic state | There is no durable receipt file or table; restart loses this process's records. |

The lower-level `AtomicStateBackend` API provides `scan(namespace)`,
`get(namespace, receipt_id)`, and
`delete(namespace, receipt_id, expected_version=record.version)`. Use the exact
backend configuration, inspect the scanned receipt payloads, and delete only
approved IDs with the version observed during review. A failed conditional
delete requires a fresh review, not an unconditional retry. The receipt adapter
itself intentionally has no delete method. Keep the namespace and reviewed IDs
explicit in any operator script; do not derive a broad wildcard from a date.

This removes selected active-store records only. Exported receipts, outcome
records, backups, Redis persistence files, replicas, and database recovery logs
need their own retention procedures. Neither transition pruning nor this manual
procedure guarantees physical erasure of every copy of a payload.
