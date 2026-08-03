# Failure & threat model for the transition / ledger core

A plain-language answer to: *what does Mycelium actually protect, what does it
not, and where is each claim proven?* This is about the **transition / action-ledger
core** (`@ledger` / `@ledger_sync`, the `ActionLedger`, resolution gates,
reconciliation, operator release). It deliberately excludes the optional guard
surface (`@protect`, `HistoryGuard`, `MessageValidator`, `@bounded`, `Session`).

The companion runbook is the README's [Operator runbook: your agent
hard-blocked](../README.md#operator-runbook-your-agent-hard-blocked). The
README is the source of truth for how the pieces are used; this file is the
honest accounting of what can go wrong and which guarantee is pinned to which
test.

> Version note: this document tracks package **v1.23.1**. The ledger-core
> guarantees below are unchanged; optional `loop_guard:` (AF-003),
> `completion:` (AF-007), and `state_authority:` are documented in the SDK
> README and are outside this core guarantee set.

---

## A. Scope

Mycelium's core promise is narrow and specific:

> **When a side-effecting tool is configured with a durable ledger and a
> transition binding, Mycelium prevents the same side effect from executing
> twice on retry, crash, or concurrent redispatch.**

"Prevent" means: at most one tool body run per transition, plus exactly one
extra run per **provably not executed** verdict (an operator release
`--verified not-executed` or a reconciler returning `NOT_EXECUTED`). The
tool's *outcome* is decided by the transition state machine, never by
blindly re-running the body.

This document is **explicitly out of scope** for:

- **LLM hallucination, prompt injection, or "is the operator *allowed* to
  release this?"** — see [release authority](../README.md#operator-runbook-your-agent-hard-blocked)
  for the honesty model (`--by` is an audit stamp, not authentication).
- Budget / runaway-loop control *unless* optional `loop_guard:` (AF-003) is
  configured — that guard halts consecutive identical action hashes across
  distinct `tool_call_id`s. Without it, a tool that legitimately runs 1,000
  times under 1,000 distinct transition keys is *not* treated as a duplicate.
- **Premature “done” / incomplete checklists** *unless* optional
  `completion:` (AF-007) is configured — the ledger does not gate run-exit.
  With `completion:`, unmarked **required** subtasks refuse terminal;
  unmarked **optional** only warn. Does **not** judge open-ended user goals
  (AF-005).
- **State authority / superseded checkpoints** *unless* optional
  `state_authority:` is configured — the ledger does not refuse a *new*
  `tool_call_id` derived from a stale checkpoint. The pre-claim
  `StateAuthority` gate (freeze `state_ref` at decide, compare at execute)
  is documented in the SDK README; not part of the ledger core set below.
- The optional `@protect` / `HistoryGuard` / `MessageValidator` / `@bounded`
  / `Session` / `loop_guard` / `completion` / `state_authority` features
  (documented in the catalog and SDK README; not part of the ledger core
  guarantee set below).

---

## B. Threat / failure actors

The actors this core defends against (and the ones it assumes are honest):

| # | Actor | What could go wrong |
|---|-------|---------------------|
| 1 | **Buggy agent redispatch** | The framework retries a tool call while the first attempt is still running or after it completed — the side effect runs twice. This is the [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417) shape. |
| 2 | **Two concurrent workers** | Worker A and Worker B both claim the same transition; both run the tool body. |
| 3 | **Crash mid-effect** | The process is killed after the provider accepted the charge but before `complete()` — the transition is ambiguous. |
| 4 | **Storage outage** | Redis / Postgres / the file backend is down at claim, complete, or failure-recording time. |
| 5 | **Stalled worker wake** | A worker is paused (GC, partition, stopped auto-renew), its lease expires, then it wakes late with a stale snapshot and tries to resolve the entry. |
| 6 | **Operator with backend access** | Anyone who can write to the ledger backend can release, stamp a resolution, or assert worker death. |
| 7 | **Provider indexing lag** | A provider (e.g. Gmail sent-log) hasn't made a sent message visible yet — a naive reconciler would say "never sent". |
| 8 | **Caller tweaking args / keys** | A caller changes a "fluff" argument to re-mint a different transition key and dodge an in-flight lease, starting a second side effect. |

Actors **assumed honest** (defended by contract, not cryptography): the
`Reconciler` you wire in, the operator you put on-call, and the provider your
reconciler queries.

---

## C. What we protect (guarantees)

Each guarantee below is grounded in shipped behavior and has a test row in
[section E](#e-guarantee--test-map). "Where documented" points at the README
section; the tests are concrete `file::test_name` entries.

1. **Atomic first claim, single winner.** Exactly one worker wins the initial
   claim; peers are routed to `POLL` (wait for the winner) or `RETURN`
   (already done), never to a second execution.
   *Where:* [Resolution gates](../README.md#resolution-gates),
   [Backend implementation](../README.md#backend-implementation).

2. **CAS on every terminal-outcome write.** `complete()` / `fail()` /
   `mark_blocked()` / `mark_unknown()` are compare-and-swap writes that only
   succeed from `IN_FLIGHT`. A resolved transition
   (`COMPLETED` / `BLOCKED` / `UNKNOWN` / `FAILED_*`) refuses all four.
   *Where:* [Atomicity contract](../README.md#atomicity-contract-v118),
   [Transition matrix](../README.md#transition-matrix-rejected-transitions).

3. **Owner fencing.** The wrapper captures the worker's identity and passes it
   as `_expected_owner`; a different worker's write to the same entry is
   refused. A stale worker cannot silently overwrite a real `COMPLETED` (or a
   real failure).
   *Where:* [Owner fencing](../README.md#owner-fencing).

4. **Single-winner reclaim.** After a lease expires, at most one worker
   reclaims the transition; the loser polls or hard-blocks — never both run.
   *Where:* Lease validity / auto-renew → [Resolution gates](../README.md#resolution-gates).

5. **Stale-snapshot guard.** A hard-block decision is re-checked against the
   durable record. If a re-read finds the transition `IN_FLIGHT` with a live
   lease, the claim path returns to the poll loop instead of raising, and
   `mark_blocked` is never applied to an entry whose lease is currently held.
   *Where:* [NOT_EXECUTED reset CAS](../README.md#not_executed-reset-cas-v118).

6. **Dual `NOT_EXECUTED` verdicts → at most one re-execution.** When two
   reconcilers (or operators) both prove "not executed", the CAS loser reads
   the winner's entry and polls it to completion instead of re-running the
   tool a second time. A 25-redispatch storm cannot double-charge.
   *Where:* [NOT_EXECUTED reset CAS](../README.md#not_executed-reset-cas-v118).

7. **Fail-closed when durable storage is unavailable for a critical write.**
   A failed `claim()` means **the tool never runs**. A failed `complete()` /
   failure-recording propagates the storage error and leaves the entry
   `IN_FLIGHT` → `EXPIRED` → hard-block/reconcile. Storage errors never
   masquerade as tool errors, and a tool exception is never masked by a
   storage error.
   *Where:* [What happens when storage is down](../README.md#what-happens-when-storage-is-down).

8. **Hard-block on ambiguous mutation without proof.** An ambiguous mutating
   transition (`maybe_crossed`, `crossed`, `FAILED_AFTER_EFFECT`, `EXPIRED`
   past the boundary, or `UNKNOWN` with no reconciler) hard-blocks instead of
   re-executing. The tool body never runs again until a reconciler proves
   `NOT_EXECUTED` or an operator releases it.
   *Where:* [Resolution gates](../README.md#resolution-gates),
   [Marking the side-effect boundary](../README.md#marking-the-side-effect-boundary-side_effect).

9. **Reconciliation is fail-closed.** No `external_operation_ref`, no
   reconciler, or a reconciler that raises or times out all resolve to a
   hard-block — an exception in the reconciler never propagates to the caller.
   *Where:* [Reconciling automatically](../README.md#reconciling-automatically-reconciler).

10. **Side-effect boundary classification is monotonic and durable.**
    `not_crossed → maybe_crossed → crossed` only ever moves forward. Because
    `maybe_crossed` is written durably *before* the external call, a crash
    mid-call hard-blocks instead of double-spending.
    *Where:* [Marking the side-effect boundary](../README.md#marking-the-side-effect-boundary-side_effect).

11. **Gmail reconciler is conservative about indexing lag.** 0 or 2+ sent-log
    matches → `UNKNOWN` (hard-block, operator release); exactly 1 → `COMPLETED`;
    missing ref → `UNKNOWN`. Zero matches is "not yet visible," never a blind
    `NOT_EXECUTED`.
    *Where:* [Gmail sent-log reconciler](../README.md#gmail-sent-log-reconciler-gmailreconciler).

12. **Operator release is one-shot and fail-closed.** `--verified not-executed`
    grants **exactly one** re-execution; `--verified completed` returns the
    recorded result without re-running. Releases are refused on unknown
    request ids, already-terminal transitions, and `IN_FLIGHT` entries with a
    held lease. Entries are never deleted — the resolution is stamped on the
    durable record.
    *Where:* [Operator runbook](../README.md#operator-runbook-your-agent-hard-blocked).

13. **Worker-death gate (opt-in).** With `reclaim_requires_death_signal: true`,
    reclaim/release of an `EXPIRED` entry requires an operator assertion that
    the worker is dead; a live heartbeat within the grace window blocks
    `mark-dead` (unless overridden with direct evidence).
    *Where:* [Assert worker death](../README.md#operator-runbook-your-agent-hard-blocked).

14. **Provider idempotency-key enforcement (opt-in).** With
    `provider_idempotency_key_param` declared, a retry that presents a
    different or missing key hard-blocks (it would risk a second, undeduped
    effect). The declared key is excluded from the transition-key fingerprint
    so key-swapping retries are caught, not silently re-keyed.
    *Where:* [Enforcing the same provider idempotency key](../README.md#enforcing-the-same-provider-idempotency-key-provider_idempotency_key_param).

15. **Key derivation is deterministic and sound.** Identical redispatches map
    to one key; changing a *real* argument produces a new key; `tool_call_id`
    binds the dispatch identity but is excluded from the args fingerprint.
    *Where:* [Transition identity and the `request_id` caveat](../README.md#transition-identity-and-the-request_id-caveat).

16. **Task-level idempotency.** The task ledger returns a stored result for a
    repeated task id (deduplicated across processes when storage is durable).
    *Where:* [Quickstart: task-level idempotency](../README.md#quickstart-task-level-idempotency).

---

## D. What we do not protect

These are documented behaviors — called out so nobody reads a stronger promise
than the code makes.

- **Release authorization.** Anyone who can write to the ledger backend can
  release a transition. `--by` is an **audit stamp**, not authentication. This
  is an honesty model, not a safety guarantee. See the runbook's
  [warning](../README.md#operator-runbook-your-agent-hard-blocked). *(Not a
  guarantee — see `test_operator_release.py` for the one-shot/fail-closed
  semantics, and `test_audit_receipt.py::test_tampered_receipt_fails_verification`
  for tamper-evidence when receipts are enabled.)*
- **Identity-conflict rejection.** Same `request_id` + changed args = a
  *new* transition, by design. Two dispatches with the same ticket but
  different instructions are different operations. An opt-in rejection mode
  has been discussed but is **not shipped**; the current contract is pinned by
  `tests/test_mengchheang_public_repro.py::test_semantic_identity`
  (continuity-harness scenario). Treat this as an intentional non-guarantee.
- **Budget / runaway loops (without `loop_guard:`).** If a caller produces many
  distinct transition keys, the ledger does not stop the calls. Optional
  AF-003 `loop_guard:` halts consecutive identical *action* hashes (tool + args)
  across new dispatch ids; it is not a general spend budget.
- **Premature terminal (without `completion:`).** The ledger does not stop an
  agent from emitting “done” with unfinished work. Optional AF-007
  `completion:` refuses terminal when **required** checklist ids are still
  pending; it does not judge open-ended goals (AF-005) and never fires unless
  an entry point (`complete_run` / END / final-message wrap) is wired.
- **Superseded state (without `state_authority:`).** A redispatch from a stale
  checkpoint that mints a new `tool_call_id` / changed args has no prior claim
  and PROCEEDs. Optional `state_authority:` compares a frozen `state_ref` to the
  host's canonical ref before claim; see SDK README.
- **Trusting the reconciler.** If your reconciler returns `NOT_EXECUTED` when
  the effect actually happened, the runtime will re-execute once. Reconcilers
  are read-only *by contract*, not enforced by Mycelium.
- **In-memory ledgers across processes.** `storage: memory` claims are not
  durable beyond the process. Mycelium emits a warning when a side-effecting
  tool is configured with memory storage; the guard only holds within the
  process. (Out-of-scope alternative: use file/Redis/Postgres.)
- **Unclassified tools under the default `warn` policy.** A tool without a
  transition binding that fails is re-executed on reclaim (legacy behavior,
  with a one-time warning). `unclassified_policy: strict` routes them through
  a conservative `non_idempotent_mutate` binding that hard-blocks instead.
- **Temporal-style workflows.** Mycelium guards individual tool calls (and
  task ledger entries); it does not re-run a multi-step workflow graph with
  orchestrator recovery semantics.
- **The optional guard surface.** `@protect` / `HistoryGuard` /
  `MessageValidator` / `@bounded` / `Session` are documented elsewhere and are
  not part of this threat model.

---

## E. Guarantee → test map

Every guarantee in [section C](#c-what-we-protect-guarantees) maps to
concrete tests. Rows cite `tests/<file>.py::<test_name>`; parametrized tests
are cited once. "Where documented" links the README section.

| Guarantee | Where documented | Test(s) |
|---|---|---|
| Atomic first claim, single winner | README § [Resolution gates](../README.md#resolution-gates) / [Backend implementation](../README.md#backend-implementation) | `test_storage_backends.py::test_file_storage_serializes_concurrent_claims` · `test_storage_backends.py::test_redis_storage_atomic_claim` · `test_storage_backends.py::test_postgres_storage_atomic_claim`<sup>1</sup> · `test_proof_two_worker_redis.py::test_two_worker_redis_cloud_style_redispatch`<sup>2</sup> · `test_multiprocess_concurrency.py::test_two_processes_redis_contested_claim` |
| CAS on terminal-outcome writes | README § [Transition matrix](../README.md#transition-matrix-rejected-transitions) | `test_atomicity_contract.py::test_transition_matrix` · `test_atomicity_contract.py::test_concurrent_complete_race` · `test_atomicity_contract.py::test_concurrent_complete_and_fail_race` |
| Owner fencing (no silent overwrite of COMPLETED) | README § [Owner fencing](../README.md#owner-fencing) | `test_atomicity_contract.py::test_owner_mismatch_on_complete` · `test_atomicity_contract.py::test_owner_match_succeeds` · `test_atomicity_contract.py::test_wrapper_owner_fencing_prevents_stale_overwrite` · `test_atomicity_contract.py::test_stalled_worker_cannot_overwrite_completed` · `test_atomicity_contract.py::test_stalled_worker_cannot_overwrite_failed` |
| Single-winner reclaim after lease expiry | README § Lease validity / auto-renew → [Resolution gates](../README.md#resolution-gates) | `test_atomicity_contract.py::test_concurrent_reclaim_race_inmemory` · `test_atomicity_contract.py::test_concurrent_reclaim_race_redis` · `test_terminal_outcome.py::test_reclaim_after_expired_lease` · `test_side_effect_resolution.py::test_spendability_override_allows_expired_reclaim` · `test_multiprocess_concurrency.py::test_two_processes_reclaim_expired_payment_single_reexec` |
| Stale-snapshot guard (`mark_blocked` never on a held lease) | README § [NOT_EXECUTED reset CAS](../README.md#not_executed-reset-cas-v118) | `test_atomicity_contract.py::test_raise_hard_block_stale_snapshot_returns_inflight_held_lease` · `test_conformance_tsc007.py::test_case_1_in_flight_valid_lease_polls_without_reexecuting` · `test_lease_validity.py::test_auto_renew_keeps_peer_on_poll_past_original_ttl` |
| Dual `NOT_EXECUTED` → at most one re-execution | README § [NOT_EXECUTED reset CAS](../README.md#not_executed-reset-cas-v118) | `test_atomicity_contract.py::test_concurrent_reconcile_not_executed_race` · `test_atomicity_contract.py::test_concurrent_reconcile_not_executed_race_expired_seed` · `test_mengchheang_public_repro.py::test_concurrent_reconcile_not_executed` · `test_payment_provider_mock.py::test_redispatch_storm_never_double_charges` |
| Fail-closed on storage outage | README § [What happens when storage is down](../README.md#what-happens-when-storage-is-down) | `test_fail_closed_storage.py::test_claim_raises_storage_unavailable` · `test_fail_closed_storage.py::test_tool_never_runs_on_storage_down_claim` · `test_fail_closed_storage.py::test_complete_propagates_storage_error` · `test_fail_closed_storage.py::test_storage_failure_does_not_mask_tool_exception` · `test_fail_closed_storage.py::test_tool_exception_propagates_not_storage_exception` · `test_outage_redis_postgres.py::test_claim_during_outage_raises_storage_unavailable` · `test_outage_redis_postgres.py::test_complete_during_outage_keeps_inflight` · `test_outage_redis_postgres.py::test_failure_recording_outage_surfaces_original_exception` · `test_outage_redis_postgres.py::test_real_redis_entry_path_wraps_connection_error` |
| Hard-block on ambiguous mutation | README § [Resolution gates](../README.md#resolution-gates) | `test_side_effect_resolution.py::test_payment_hard_blocks_expired_lease` · `test_side_effect_resolution.py::test_crash_after_claim_before_complete_hard_blocks_redispatch` · `test_side_effect_resolution.py::test_payment_hard_blocks_failed_after_effect_retry` · `test_reconcile.py::test_hard_block_without_reconciler_still_blocks` · `test_process_kill_crash_window.py::test_kill_before_ref_recorded_hard_blocks_no_provider_lookup` · `test_payment_provider_mock.py::test_no_reconciler_hard_blocks_without_provider_evidence` |
| Resolution gate matrix (POLL / RETURN / ALLOW / HARD_BLOCK / reconcile) | README § [Resolution gates](../README.md#resolution-gates) | `test_conformance_tsc007.py` (5 cases) · `test_side_effect_resolution.py::test_resolve_side_effect_gate_matrix` · `test_read_only_resolution.py::test_resolve_read_only_gate_matrix` |
| Reconciliation fail-closed | README § [Reconciling automatically](../README.md#reconciling-automatically-reconciler) | `test_reconcile.py::test_reconcile_failure_is_fail_closed` · `test_reconcile.py::test_reconcile_skipped_without_external_ref` · `test_reconcile.py::test_reconcile_unknown_hard_blocks` · `test_outage_redis_postgres.py::test_mid_reconcile_storage_outage_fail_closed` |
| Boundary classification + monotonic, durable `maybe_crossed` | README § [Marking the side-effect boundary](../README.md#marking-the-side-effect-boundary-side_effect) | `test_side_effect_boundary.py::test_advance_boundary_is_monotonic` · `test_side_effect_boundary.py::test_side_effect_marks_maybe_crossed_midflight` · `test_side_effect_boundary.py::test_exception_inside_side_effect_marks_unknown_and_hard_blocks` · `test_side_effect_boundary.py::test_exception_before_marker_is_failed_before_effect` · `test_side_effect_boundary.py::test_mark_crossed_then_exception_is_failed_after_effect` · `test_side_effect_boundary.py::test_async_side_effect_marks_unknown_on_error` |
| Gmail reconciler matrix (0/1/2+/missing ref) | README § [Gmail sent-log reconciler](../README.md#gmail-sent-log-reconciler-gmailreconciler) | `test_gmail_reconciler.py::test_zero_matches_returns_unknown` · `test_gmail_reconciler.py::test_one_match_returns_completed` · `test_gmail_reconciler.py::test_two_matches_returns_unknown` · `test_gmail_reconciler.py::test_missing_external_operation_ref_returns_unknown` · `test_gmail_reconciler.py::test_empty_external_operation_ref_returns_unknown` |
| Operator release one-shot + fail-closed | README § [Operator runbook](../README.md#operator-runbook-your-agent-hard-blocked) | `test_operator_release.py::test_release_is_one_shot` · `test_operator_release.py::test_release_not_executed_grants_exactly_one_reexecution` · `test_operator_release.py::test_release_completed_returns_result_without_reexecution` · `test_operator_release.py::test_release_refused_while_lease_held_allowed_once_expired` · `test_operator_release.py::test_release_refused_on_completed_and_unknown_request` · `test_operator_release.py::test_keyed_mutate_still_enforces_provider_key_after_release` |
| Release stamps the record (never deleted) | README § [Operator runbook](../README.md#operator-runbook-your-agent-hard-blocked) | `test_operator_release.py::test_release_not_executed_grants_exactly_one_reexecution` (asserts `operator_resolution`/`resolved_by`) · `test_operator_release.py::test_postgres_release_not_executed_round_trip` |
| Release emits signed audit receipts when configured | README § [Operator runbook](../README.md#operator-runbook-your-agent-hard-blocked) | `test_operator_release.py::test_release_emits_audit_receipt_when_emitter_configured` · `test_audit_receipt.py::test_emitter_signs_and_verifies_tool_receipt` · `test_audit_receipt.py::test_tampered_receipt_fails_verification` |
| Worker-death gate (opt-in) | README § [Assert worker death](../README.md#operator-runbook-your-agent-hard-blocked) | `test_worker_death_signal.py::test_release_expired_refused_without_death_evidence` · `test_worker_death_signal.py::test_release_expired_allowed_with_asserted_death` · `test_worker_death_signal.py::test_mark_worker_dead_refuses_recent_heartbeat_without_override` · `test_worker_death_signal.py::test_read_only_reclaim_blocked_without_death_evidence` · `test_worker_death_signal.py::test_side_effecting_allow_blocked_without_death_evidence` |
| Provider idempotency-key enforcement (opt-in) | README § [Enforcing the same provider idempotency key](../README.md#enforcing-the-same-provider-idempotency-key-provider_idempotency_key_param) | `test_provider_idempotency_key.py::test_gate_hard_blocks_different_provider_key` · `test_provider_idempotency_key.py::test_gate_hard_blocks_missing_incoming_key` · `test_provider_idempotency_key.py::test_gate_hard_blocks_missing_stored_key` · `test_provider_idempotency_key.py::test_declared_key_is_excluded_from_transition_key` · `test_provider_key_validity.py::test_same_key_expired_ttl_hard_blocks` |
| Key derivation soundness | README § [Transition identity and the `request_id` caveat](../README.md#transition-identity-and-the-request_id-caveat) | `test_transition.py::test_same_inputs_produce_same_transition_key` · `test_transition.py::test_different_tool_call_id_produces_different_key` · `test_transition.py::test_ledger_deduplicates_by_transition_key` · `test_property_transitions.py::test_transition_key_invariants` (property test) |
| Task-level idempotency | README § [Quickstart: task-level idempotency](../README.md#quickstart-task-level-idempotency) | `test_cli_run.py::test_run_instruments_sync_tool_and_task_across_processes` |
| Single-key state machine invariants (executions ≤ 1 + not-executed verdicts; COMPLETED terminal; CAS out of IN_FLIGHT) | this doc, § C / [NOT_EXECUTED reset CAS](../README.md#not_executed-reset-cas-v118) | `test_property_transitions.py::test_transition_key_invariants` (Hypothesis, file + Redis) · `test_payment_provider_mock.py::test_redispatch_storm_never_double_charges` |

<sup>1</sup> `test_postgres_storage_atomic_claim` runs when `psycopg` is
installed and `MYCELIUM_TEST_POSTGRES_DSN` is set; it skips otherwise.
<sup>2</sup> `test_two_worker_redis_cloud_style_redispatch` needs a reachable
Redis (`MYCELIUM_TEST_REDIS_URL` or `redis://127.0.0.1:6379/15`); it skips
otherwise.

---

## F. Residual risks

Still can go wrong — even with everything above configured correctly:

- **`storage: memory` across processes.** The guard holds within one process
  only. Mycelium warns at config time; don't ship memory storage for
  multi-worker side-effecting tools.
- **A reconciler that lies.** If your reconciler returns `NOT_EXECUTED` for an
  effect that actually happened, the runtime re-executes once. Reconcilers are
  read-only *by contract*; verify them with the same care as the tools
  themselves. The Gmail reconciler's conservative "0 matches → UNKNOWN" is the
  model to copy.
- **No death-signal opt-in.** `reclaim_requires_death_signal` defaults off, so
  reclaim/release proceeds on lease expiry even if the worker is merely paused
  or partitioned. Enable the gate in production; until then, an expired lease
  is treated as dead.
- **Storage failure at the worst moment.** A `complete()` storage failure
  leaves the entry `IN_FLIGHT`; the lease then expires and the transition
  hard-blocks or waits on a reconciler. Safe (no second effect), but the
  operation may park for an operator.
- **Provider indexing lag.** A reconciler that treats "not visible" as "never
  happened" would allow a duplicate. The shipped Gmail reconciler returns
  `UNKNOWN` on 0 matches; third-party reconcilers must do the same.
- **Caller tweaking "fluff" args to escape the key.** Mycelium's compound key
  cannot, on its own, distinguish a real field change from an evasion. The
  [payment-class identity guidance](../README.md#payment-class-identity-server-authoritative)
  (server-authoritative, HMAC-derived keys) is the mitigation — the runtime
  enforces *same key on retry*, your application must mint stable, server-side
  keys.
- **Wall-clock leases.** Leases rely on `time.time()`. Clock skew can renew or
  expire leases early; extreme skew is a deployment concern, not something the
  runtime compensates for.
- **Silent duplicates are invisible without opt-in telemetry.** The guard
  prevents unauthorized re-execution, but a violation (lying reconciler,
  caller escaping the key, bug in the runtime) only surfaces as
  business-level weirdness unless you can see it. Enable `outcome_emit` and
  track the **DTTR** (`mycelium outcomes dttr`, target 0.0) to make the
  guarantee observable: it counts tool-body executions that were *not*
  authorized by a consumed `NOT_EXECUTED` verdict, over transitions that were
  long-running or redispatched. See the
  [README section](../README.md#outcome-telemetry--dttr-v120) for the exact
  definition.

---

*Docs-only change. Not a design partner endorsement; the failure model is
informed by external review (incl. the langgraph#7417 reproduction and a
semantic-identity continuity harness).*
