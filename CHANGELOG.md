# Changelog

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
