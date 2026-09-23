# Proposal: action-ledger payload controls

Status: proposed, unimplemented. This document requests design review for
[issue #82](https://github.com/mycelium-labs/mycelium/issues/82); it does not close
that issue or add configuration options. [Ledger payload storage](LEDGER_PAYLOAD_STORAGE.md)
describes current behavior and the persisted-field inventory.

The first implementation should let operators omit invocation evidence while
preserving identity checks. Result omission needs a separate, explicit replay
contract. Retention should follow after those contracts have tests.

## Current constraints

`LedgerEntry.to_dict()` includes arguments, results, errors, and transition
metadata. `ActionLedger` also uses stored arguments for correctness:
`_enforce_args_drift()` rebuilds fingerprints from `args` and `kwargs`, and
scope and dispatch checks read stored kwargs. Removing those fields today would
change conflict detection. A retained `effect_id` alone is insufficient.

Completed calls in the synchronous and asynchronous execution paths return
`existing.result`. There is no distinct representation for an omitted result
versus a legitimate `None` result. Audit receipts copy entry inputs, outputs,
and errors into separately signed records. Redis recovery tombstones retain a
second payload copy and can restore expired primary keys.

## Proposed contract

Keep today's storage and replay behavior when no payload policy is configured.
The initial policy should apply to ActionLedger only. TaskLedger needs its own
follow-up because it has separate replay paths.

Treat the following as separate concerns, even if they remain in one serialized
record:

- Identity and transition control: request/effect IDs, aliases, tool and scope,
  dispatch identity, argument-conflict fingerprints, fencing, outcome, and
  recovery/provider metadata.
- Invocation evidence: stored arguments, keyword arguments, error text, and
  decision evidence that may contain payload data.
- Result evidence and replay: whether a stored return value is available and
  whether policy has transformed it.

Before allowing argument omission, persist versioned identity metadata that
reproduces current request-ID, alias, scope, and argument-conflict decisions.
Preserve provider-key exclusions and the secret, entity, destructive-confirm,
and use-time contributions in the existing fingerprint helpers. Do not hash a
newly redacted evidence copy and assume it represents the original identity.
The comparison format must remain stable across workers and restarts; a policy
change must not silently make old and new fingerprints incomparable.

Fingerprints are not encryption. Low-entropy values may be guessable, and scope
IDs, provider references, operator reasons, and decision records may themselves
be sensitive. Document the retained fields and reject attempts to redact fields
required for the configured safety contract. This proposal makes no promise
that omitting arguments removes every sensitive value.

## Policy and sanitizer ordering

The issue's `store_args`, `store_result`, and `redact_fields` names are candidate
API names, not supported settings. Start with argument omission and explicit
field-path redaction. Define positional-argument paths, nested objects, lists,
missing fields, and replacement values before accepting configuration. Do not
ship arbitrary callbacks or encryption hooks in the first change.

Derive identity through the existing identity rules before optional payload
suppression. Build a separate evidence copy using existing sanitizers, then
apply the new payload policy. Never mutate the arguments passed to the tool.
Apply output/error policy before persistence and receipt signing. Give every
entry a durable indication of which evidence was omitted or transformed; empty
containers and `None` must not act as omission markers.

A shared policy boundary must cover decorator and configured-wrapper execution,
sidecar operations, completion/failure, reconciliation, and operator release.
Direct storage writes currently accept caller-built entries. Either enforce the
policy there too or document and test the low-level bypass explicitly. Do not
advertise backend-wide protection while some write paths retain raw payloads.

## Result replay

For an opted-in policy, a completed call whose result was omitted must remain
completed. A duplicate must never rerun the tool to recover its return value.
Recommend a typed replay-unavailable error carrying the request/effect identity
and completed outcome. The error means the effect completed but its return value
is unavailable; retrying execution is not a remedy. A legitimate stored `None`
still replays normally.

The first caller can receive the live tool result while storage omits it.
Coordinating callers and retries receive the unavailable outcome. If the policy
transforms result evidence, recommend the same unavailable behavior rather than
returning a redacted object as the original tool result. Existing sanitizer
behavior remains unchanged without the new policy. Review Python, sidecar, and
TypeScript/Go response contracts together before enabling result omission.

## Copies and retention

Use the same effective payload policy for action receipts, including operator
release receipts, before signing. Existing signed receipts cannot be rewritten
without invalidating their signatures. Their retention and deletion require a
separate documented procedure. Archives, exports, backups, and application logs
also need operator-managed retention; ledger deletion cannot erase those copies.

Keep payload expiry separate from transition deletion. A future payload-expiry
operation should clear eligible evidence while retaining verified identity,
outcome, and recovery data. It must update Redis primary keys and recovery
tombstones consistently so restoration cannot resurrect removed payloads.
Exclude unresolved transitions initially; payload removal may destroy evidence
needed for reconciliation.

Existing `prune_transitions()` deletes whole entries. Its default outcomes are
`COMPLETED` and `FAILED_BEFORE_EFFECT`, but deleting a completed row also removes
deduplication history and can permit a later dispatch to execute again. No
payload-retention feature should imply otherwise. Keep whole-entry pruning
explicit, with dry-run review and a documented deduplication horizon.
`retention_seconds` currently supplies a prune window, not a background job.

## Migration and acceptance

Introduce a new entry schema version for explicit identity and payload-state
metadata. Read legacy entries with current behavior. Older runtimes already
reject future schema versions; document upgrade order and disallow mixed-version
writers before emitting new rows. Backfill identity while legacy evidence is
available, verify equivalent decisions, then allow suppression. If a legacy row
cannot supply sufficient identity, retain it or block migration for operator
review. Do not invent an identity from incomplete evidence.

Stage acceptance tests as follows:

1. Prove default behavior and legacy replay unchanged. Check same-request drift,
   scope changes, effect aliases, provider-key mismatches, and sanitizer-aware
   identity across restart and concurrent claims.
2. Exercise argument omission and redaction through all supported write paths
   against memory, file, SQLite, Redis, and Postgres. Inspect stored records and
   receipts, including Redis restoration, rather than only exported output.
3. Verify one execution for omitted/transformed results, legitimate `None`
   replay, sync/async waiters, and sidecar/client handling of unavailable replay.
4. Test migration failures, stale workers, and payload-expiry races with active
   transitions before adding retention enforcement.

Maintainer decisions still needed: the exact policy/path syntax, whether result
omission belongs in the first release, the replay-unavailable API, and how long
operators must retain identity after evidence expires. Arbitrary encryption
hooks, automatic whole-entry deletion, and TaskLedger policy are deferred.
