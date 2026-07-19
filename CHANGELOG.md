# Changelog

## 1.9.1 (2026-07-19)

Patch: docs sync, a flaky-test fix, and the TSC-007 transition-sufficiency conformance suite. No new schema or policy concepts.

### Docs
- Fix root `README.md` "What it does" heading to v1.9.x (was v1.8.x).
- Bump README, SDK README, handbook banner, and version source strings to v1.9.1.

### Tests
- Fix flaky `test_*_read_only_reclaims_expired_lease` (file + Redis) by raising `lease_ttl` from 0.05s to 1.0s so reclaim logic is exercised without a race against the poll loop.
- Add `tests/test_conformance_tsc007.py` — five-case transition-sufficiency suite mirroring Tuttotorna spec TSC-007 / langgraph#7417. Asserts `must_not_execute_again` for cases 1, 2, 4, 5 and re-execution for the lone safe-retry case 3. No product code changes.

### Packaging
- Ignore `.opencode/` and `pitch/` from the public repo.
- Commit `AGENTS.md` (root agent-instructions file).

## 1.9.0 (2026-07-19)

Ship the `SOFT_BLOCK` gate for read-only tools. An ambiguous `UNKNOWN` / `BLOCKED` terminal outcome on a reversible read no longer polls to a `LedgerPollTimeoutError`; it resolves through a dedicated read-only gate.

### Read-only SOFT_BLOCK

- New `SOFT_BLOCK` member on `TransitionGate` and a `resolve_read_only_gate(entry)` resolver describing the full read-only taxonomy: `COMPLETED` → `RETURN`, `IN_FLIGHT` → `POLL`, `EXPIRED` / `FAILED_BEFORE_EFFECT` / `FAILED_AFTER_EFFECT` → `RECLAIM`, `BLOCKED` / `UNKNOWN` → `SOFT_BLOCK`.
- Because re-running a read-only tool is always safe, a `SOFT_BLOCK` resolves **by default to a retry**: the ambiguous entry is reset to a fresh in-flight claim and the tool runs exactly once more.
- Opt into deferral with `ActionLedger(defer_read_only_unknown=True)` (or the `@ledger` / `@ledger_sync` `defer_read_only_unknown=` argument). The claim then raises the new `LedgerSoftBlockError` so an expensive read can be deferred and retried later by the caller (cost-dependent) instead of re-executing immediately.
- `LedgerSoftBlockError` is a *deferral*, not a terminal stop — distinct from the payment/non-idempotent `LedgerHardBlockError`, which still requires manual reconciliation. Side-effecting `UNKNOWN` resolution is unchanged (still hard-blocks / reconciles).
- Export `LedgerSoftBlockError` from the package root. Works in sync and async claim paths.

## 1.8.0 (2026-07-19)

Enforce `retry_only_with_same_provider_idempotency_key` instead of trusting it. When a tool opts in, a retry is allowed only if it provably reuses the same provider idempotency key; otherwise it hard-blocks.

### Provider idempotency key enforcement

- New opt-in `provider_idempotency_key_param` on the transition binding (and `provider_idempotency_key_param:` in YAML) naming the kwarg that carries the provider idempotency key.
- New durable `provider_idempotency_key` on `LedgerEntry`, captured at claim time from that kwarg (serialized across all backends; old records default to `None`, no migration).
- Gate change: for `retry_only_with_same_provider_idempotency_key` on a `keyed_mutate` / `idempotent_mutate` tool that failed before the effect, the retry is `ALLOW` only when the incoming key equals the stored key; a missing or different key is `HARD_BLOCK`.
- The declared key is excluded from the transition-key fingerprint, so a retry that changes the key still maps to the same transition (and is caught) rather than silently forking a new one.
- **Backward compatible / opt-in**: tools that do not declare the param keep the old cooperative behavior (retry allowed, key trusted). Works in sync and async claim paths.

## 1.7.0 (2026-07-19)

Add the automated reconciliation loop (Phase 2): when an ambiguous transition recorded an `external_operation_ref`, a `Reconciler` can query the provider and resolve it automatically instead of hard-blocking for a human.

### Reconciliation

- New `Reconciler` protocol with a read-only `reconcile(entry) -> ReconcileResult` (and optional `reconcile_async` for async tools). Implementations look up `entry.external_operation_ref` at the provider and must never create, mutate, or retry the effect.
- New `ReconcileResult` / `ReconcileStatus` with three outcomes:

| Reconcile result | Effect on the transition |
|------------------|--------------------------|
| `COMPLETED` | marked completed with the reconciled result; redispatch returns it, **no re-execution** |
| `NOT_EXECUTED` | reset to a fresh in-flight claim; the tool runs **exactly once** more |
| `UNKNOWN` | hard-block for manual reconciliation (unchanged behavior) |

- Wire a reconciler via `ActionLedger(reconciler=...)` or the `@ledger` / `@ledger_sync` `reconciler=` argument.
- The reconciler is only consulted when a side-effecting transition would otherwise hard-block **and** an `external_operation_ref` is present.
- **Fail-closed**: a missing ref, no reconciler, or a raising/timing-out reconciler all resolve to hard-block. A reconcile exception never propagates.
- Export `Reconciler`, `ReconcileResult`, `ReconcileStatus` from the package root.

## 1.6.0 (2026-07-19)

Add `external_operation_ref` — the provider's handle for a side effect — so ambiguous transitions can be reconciled against the provider (Phase 1: record + surface; automated reconcile lands next).

### External operation ref

- New durable `external_operation_ref` field on every `LedgerEntry` (serialized across memory/file/redis/postgres; old records default to `None`, no migration).
- New `record_external_operation(ref)` marker (uses the active-transition context, sibling to `side_effect()` / `mark_crossed()`) and `ActionLedger.attach_external_operation_ref()`. `ref` is a provider id (e.g. Stripe `pi_...`) or the idempotency key sent to the provider.
- The ref survives an ambiguous failure (`UNKNOWN` / `FAILED_AFTER_EFFECT` / `maybe_crossed`) and is included in the `LedgerHardBlockError` message, so a manual reconcile has the provider handle instead of nothing.
- Export `record_external_operation` from the package root.

### Not in this release (planned)

- Automated provider reconcile loop (`Reconciler` protocol; resolve `UNKNOWN` → `COMPLETED`/retry by querying the provider) — next minor.

## 1.5.0 (2026-07-18)

Complete the `maybe_crossed` boundary lifecycle so post-effect failures stop being misclassified as retry-safe.

### Side-effect boundary marker

- New `side_effect()` context manager (plus `mark_maybe_crossed()` / `mark_crossed()`) wraps the external operation of a side-effecting tool. On enter the durable entry advances to `maybe_crossed`; on clean exit to `crossed`. Boundary only ever moves forward.
- Failure classification now reads the boundary instead of always recording `FAILED_BEFORE_EFFECT`:

| Boundary at failure/crash | Terminal outcome | Redispatch |
|---------------------------|------------------|------------|
| `not_crossed` | `FAILED_BEFORE_EFFECT` | retry if policy allows |
| `maybe_crossed` | `UNKNOWN` | hard-block → reconcile |
| `crossed` | `FAILED_AFTER_EFFECT` | hard-block |

- Because `maybe_crossed` is persisted before the external call, a crash mid-call leaves the entry ambiguous and a redispatch hard-blocks instead of re-executing. Fixes the common case where an effect succeeded but downstream code (e.g. response parsing) threw, previously logged as never-happened.
- Backward compatible: tools that don't use the marker keep `not_crossed` and behave exactly as before. Works in sync and async tools.

### API

- Export `side_effect`, `mark_maybe_crossed`, `mark_crossed` from the package root
- New `ActionLedger.advance_boundary()` (monotonic) and `get_active_transition()`

## 1.4.0 (2026-07-17)

Ship `spendability` as an orthogonal axis on the transition binding (minor: new policy field; existing YAML keeps class-derived defaults).

### Spendability

Per-tool values (optional YAML override; defaults from `side_effect_class`):

| Value | Meaning | Default for |
|-------|---------|-------------|
| `multi_use` | same intent may produce effects again | `read`, `idempotent_mutate` |
| `single_use` | one effect; COMPLETED returns stored result; ambiguity hard-blocks | `keyed_mutate`, `non_idempotent_mutate` |
| `non_replayable` | under ambiguity, hard-block / reconcile | `irreversible` |

Gate behavior: expired leases with `not_crossed` may reclaim only when spendability is `multi_use` and retry permission is `safe_retry`. `single_use` / `non_replayable` hard-block ambiguous/expired states. Same transition key still returns the stored COMPLETED result for all spendability values (a new spend needs a new key).

### Templates / API

- Full YAML template documents spendability defaults and optional per-tool override
- Export `Spendability` from the package root; parse via `spendability:` on tools

## 1.3.4 (2026-07-16)

Scaffold and docs polish for the five-class `side_effect_class` model.

### Templates

- Full YAML template (`mycelium init --full`) rewritten as a fill-in reference: required/optional legend, allowlist-first wire-up, storage enums once at the top, empty `tools:` / `tasks:` (stubs as comments so `registry.auto` cannot allowlist placeholders)
- Clarify `mycelium init` = on-ramp, `--full` = reference, `--minimal` = smaller multi-guard
- TODOs for `agent_id` / `policy_version`; templatified ledgers, state_flush, audit_receipt

### Docs

- README, SDK README, handbook, and CLI help describe the init on-ramp vs `--full` reference split

## 1.3.3 (2026-07-16)

Improve `side_effect_class` to five **effect-semantic** buckets for retry/redispatch policy (not business-domain labels).

### Side-effect classes

Canonical values:

| Class | Meaning | Default on ambiguity |
|-------|---------|----------------------|
| `read` | no external mutation | poll / reclaim / retry |
| `idempotent_mutate` | mutation; retry-safe as-is | reclaim if not crossed |
| `keyed_mutate` | safe only with same provider idempotency key | hard-block unless keyed retry |
| `non_idempotent_mutate` | second call = second effect | hard-block / reconcile |
| `irreversible` | no compensation | hard-block → human |

Legacy names still parse: `read_only`, `idempotent_write`, `external_api_mutation`, `non_idempotent_write`, `payment`, `email`, `subagent`, `onchain_action`.

### Docs and templates

- Quickstart / full / minimal YAML templates use the five canonical classes
- README and handbook version bump to v1.3.3

## 1.3.2 (2026-07-15)

Transition-envelope hardening: align first-run UX with v1.3, prove crash and durable-backend behavior, and fix a public export defect. No new transition schema fields or policy concepts.

### Onboarding and demos

- `mycelium init` quickstart template now includes `transition:` and `side_effect_class: subagent` instead of legacy ledger-only config
- `mycelium demo` exercises the v1.3 transition envelope (`load_config` + `@config.apply`) instead of the v1.2 `@ledger_sync()` path
- CLI and proof tests assert that scaffolded config and demo output use the transition model

### Correctness proofs

- Add crewAI#5802-style crash-after-claim test: expired in-flight side-effecting transition hard-blocks and does not re-execute through `@ledger_sync`
- Extend file and Redis storage tests for transition hard-block, read-only reclaim, and completed read return (Postgres remains opt-in via `MYCELIUM_TEST_POSTGRES_DSN`)

### API and docs

- Export `derive_transition_key` from the package root (it was listed in `__all__` but not imported)
- Identify the published package as v1.3.2 in README, SDK docs, and handbook

### Still deferred (not in this patch)

- `spendability`, `external_operation_ref`, provider idempotency key flow, mid-flight `maybe_crossed` updates

## 1.3.1 (2026-07-06)

Patch release fixing CI and PyPI packaging for v1.3.0.

- Fix duplicate `mycelium/fixtures` path in wheel build (PyPI publish failed on [v1.3.0 tag](https://github.com/mycelium-labs/mycelium/actions/runs/28768287204))
- Fix Ruff lint errors blocking CI (import order, unused test variables)
- Add `StrEnum` compatibility shim for Python 3.10

## 1.3.0 (2026-07-06)

Transition envelope: side-effect classification, rich idempotency keys, and resolution rules that respond to post-v1.2 community feedback — especially [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417) (duplicate tool execution on checkpoint redispatch) and [crewAI#5802](https://github.com/crewAIInc/crewAI/issues/5802) (crash between claim and complete).

### Why v1.3

After v1.2 shipped, feedback converged on a few gaps:

- **Redispatch is not a fresh action** ([@Correctover](https://github.com/langchain-ai/langgraph/issues/7417#issuecomment-4861603050)): frameworks often treat “tool execution started” the same as “completed and persisted.” On LangGraph retry, the same tool call can run twice unless idempotency lives outside graph state.
- **Read-only ≠ side-effecting** ([@Tuttotorna](https://github.com/langchain-ai/langgraph/issues/7417#issuecomment-4859465734)): duplicate reads are wasteful but recoverable; duplicate payments, writes, emails, or subagent spawns are unsafe unless terminal state and side-effect boundary are known first.
- **`LedgerPendingError` is the wrong default for reads** ([#7417](https://github.com/langchain-ai/langgraph/issues/7417)): in-flight duplicates should poll and return the cached result, not fail the run.
- **Stale in-flight claims need leases, not blind reclaim** ([#5802](https://github.com/crewAIInc/crewAI/issues/5802)): a worker crash after claim but before complete must reconcile — not silently re-execute a side effect.

v1.3 addresses these with a phased envelope: classify tools, hash a durable transition key, then resolve duplicates by outcome — not by re-running blindly.

### Transition envelope

- Rich **`transition_key`** — SHA-256 of scope (`thread_id`, `run_id`, `node`), tool, args fingerprint, `side_effect_class`, `agent_id`, and `policy_version` (not only `tool_call_id`)
- **`SideEffectClass`** per tool: `read_only`, `idempotent_write`, `non_idempotent_write`, `payment`, `email`, `subagent`, `external_api_mutation`, `onchain_action`
- **`TerminalOutcome`** on ledger entries: `IN_FLIGHT`, `COMPLETED`, `FAILED_BEFORE_EFFECT`, `FAILED_AFTER_EFFECT`, `EXPIRED`, `BLOCKED`, `UNKNOWN`
- **`SideEffectBoundary`**: `not_crossed`, `maybe_crossed`, `crossed` — updated on complete / fail-after-effect
- **`RetryPermission`** per tool (YAML override or class default): `safe_retry`, `retry_only_with_same_provider_idempotency_key`, `manual_reconciliation_required`, `never_retry_automatically`

### Resolution paths

- **`read_only`** tools: poll in-flight, reclaim expired leases, retry failed-before-effect — no `LedgerHardBlockError`
- **Side-effecting** tools: return completed, poll in-flight, hard-block ambiguous states — raises `LedgerHardBlockError` instead of auto-reclaiming failed payment/write entries (v1.2 behavior)
- **Legacy path**: configs without `transition:` keep v1.2 `@ledger` behavior unchanged

### Config (YAML)

```yaml
transition:
  agent_id: payment-agent
  policy_version: "2026.07.1"
  lease_ttl: 3600
  poll_interval: 0.05
  poll_timeout: 300

tools:
  send_payment:
    side_effect_class: payment
    retry_permission: manual_reconciliation_required
```

Ledgered tools require `side_effect_class` when `transition:` is configured.

### Breaking changes

- **`audit_receipt.agent_id` removed** — set `transition.agent_id` instead (required when audit receipts are enabled)
- New exceptions: `LedgerHardBlockError`, `LedgerPollTimeoutError`

### Not in v1.3 (planned)

- `spendability`, `external_operation_ref`, provider idempotency key flow, mid-flight `maybe_crossed` updates

## 1.2.0 (2026-06-30)

- `mycelium demo`: terminal demo of langgraph#7417 duplicate tool execution
- `mycelium init` defaults to LangGraph quickstart template; use `mycelium init --full` for all guards

## 1.1.1 (2026-06-30)

- PyPI description and README use plain language

## 1.1.0 (2026-06-30)

First public PyPI release as **`mycelium-runtime`** (`pip install mycelium-runtime`).

### Packaging
- PyPI distribution renamed from `mycelium-sdk` (name taken) to `mycelium-runtime`
- Python **3.10+** support (was 3.12-only in early releases)
- GitHub Actions publish workflow (tag `v*` → PyPI)

### Ledger storage backends
- **File**: `fcntl` locking for multi-process safety on a single host
- **Redis**: atomic `SET NX` claim + in-flight TTL (multi-worker)
- **Postgres**: `INSERT ... ON CONFLICT` claim (audit/compliance)
- Optional extras: `mycelium-runtime[redis]`, `mycelium-runtime[postgres]`

## 1.0.0 (2026-06-29)

First production release. Context guards, tool boundaries, and action idempotency with YAML-first integration.

### Requirements
- Python **3.10+** (tested on 3.10, 3.11, 3.12, 3.13)

### Context
- `@protect` / `protect_sync`: TTL cache with per-entity keys
- `Session`: per-run cache isolation
- `MessageValidator`: broken transcript detection and repair
- `HistoryGuard`: token limits and silent drop detection

### Tool boundaries
- `@bounded` / `bounded_sync`: input/output validation and scope gates
- `ToolRegistry`: allowlist enforcement
- `ToolRunner`: structured LLM retry on boundary failures

### Action idempotency
- `ActionLedger` / `@ledger`: tool-level idempotency
- `TaskLedger` / `@task_ledger`: task-level idempotency
- `StateFlush`: partial state persistence on cancel/disconnect/error
- `AuditReceipt`: HMAC-signed tamper-evident action receipts

### Developer experience
- YAML config with global sections: `action_ledger`, `task_ledger`, `state_flush`, `audit_receipt`
- `mycelium init`: scaffold `mycelium.yaml` from bundled templates (PyPI users)
- `config.instrument(module)`: wrap tools and tasks in one call
- `config.prepare_messages()`: message validation + history guard + auto state recording
- `config.run(run_id)`: Session + StateFlush combined
- `registry.auto: true`: allowlist from configured tools
- `ledger: true` inherits global storage settings
