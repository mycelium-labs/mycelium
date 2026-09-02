# Changelog

Release policy: **batch; calm over velocity.** Prefer one coherent cut over many
small PyPI versions. Pre-release checklist: [sdk/docs/RELEASE.md](sdk/docs/RELEASE.md).

## Unreleased

### Fixed

- Require `state_flush.flush_on_complete` to be a YAML boolean instead of
  accepting truthy values such as quoted `false`.
- Reject non-finite and boolean authority-window clock-skew tolerances during
  policy construction and YAML configuration loading.
- Require audit receipt flags in tool, task, and global configuration to be
  YAML booleans instead of accepting truthy values such as quoted `false`.
- Validate webhook exporter timeouts as finite, positive numbers and reject
  booleans in both direct construction and YAML configuration.
- Reject non-finite and boolean use-time currency freshness windows during
  fact construction and YAML configuration loading.
- Compare contention and concurrent-reconciliation worker results as structured
  JSON so PostgreSQL JSONB key reordering cannot cause false verification failures.
- Include the official `mycelium-setup` coding-agent skill in built wheels and
  add `mycelium skills install` for offline, idempotent installation into a
  project or user skill catalog.
- Allow custom runtimes launched by `mycelium run` to declare a trusted,
  idempotent completion adapter installer that runs before production terminal
  protection is validated.
- Make the setup skill classify features as enabled, deferred for host work,
  deferred for operator input, or intentionally not applicable instead of
  implying every feature belongs in every project.
- Clarify that `budget.max_steps` counts protected tool calls and instrumented
  LLM turns rather than business workflow outcomes, including the unit in the
  typed schema, generated reference, template guidance, and Doctor output.
- Document and test host-owned, per-run entity policies for exact destinations
  selected dynamically by a trusted orchestrator, and teach the bundled setup
  skill not to replace them with model-controlled or wildcard allowlists.

## 1.38.0 (2026-08-29)

### Added

- Stable public API organization through the recommended package root plus
  `mycelium.runtime`, `mycelium.integrations`, `mycelium.experimental`, and
  private `mycelium._internal` namespaces. A reviewed API snapshot,
  compatibility tests, and deprecation metadata now guard accidental breakage.
- Generated configuration reference documentation and validated example YAML,
  project-aware `mycelium init --detect`, and opt-in conservative
  `mycelium doctor --fix` support limited to version and IDE-schema metadata.

## 1.37.0 (2026-08-29)

### Added

- Bounded transition lifecycle operations: Postgres connection pooling and
  server-side filtered keyset pagination, Redis status/start/finish sorted-set
  indexes with legacy backfill, configurable retention age, sanitized NDJSON
  export/archive, and safe-by-default `mycelium transitions prune --dry-run`.

- Distributable setup skill and configuration contract improvements, including
  CRM-aware discovery guidance, versioned config schema validation, a schema CLI,
  richer templates, and stricter fail-closed verification.

## 1.36.0 (2026-08-27)

MINOR: adds an opt-in deployment verification layer for teams that want to
exercise Mycelium across real shared infrastructure before approving a change.
Ordinary `mycelium verify --scenario ...` remains synthetic and unchanged.

### Added

- Optional `mycelium verify --cluster` mode for Redis/PostgreSQL deployments.
  It requires explicit `verify.cluster.enabled: true` and
  `deployment.topology: multi_node`, launches exactly two subprocess workers,
  interrupts their real backend connection through a verifier-owned TCP proxy,
  hard-kills worker A after a sandbox provider operation, and requires worker B
  to reconcile the recorded operation without executing the provider body a
  second time.
- Fail-closed `http_json` sandbox-provider adapter using idempotent
  `PUT /operations/{operation_id}` and read-only
  `GET /operations/{operation_id}` calls. Provider URLs, tokens, and attestation
  keys are read from named environment variables and excluded from reports.
- Versioned HMAC-SHA256 deployment attestations binding the configuration
  digest, backend, topology, namespace, provider adapter, worker count, cleanup,
  and the complete required check set. CI and change-approval systems can
  validate a report with `mycelium verify --verify-attestation FILE
  --attestation-key-env ENV`.
- Cluster configuration examples, safety limitations, dedicated unit tests, and
  a live Redis integration test covering interruption, hard worker death,
  reconciliation, duplicate prevention, cleanup, and signature verification.

## 1.35.0 (2026-08-25)

MINOR: one coherent trust-boundary batch: closes the remaining effect-commit
architecture gaps, authenticates operator releases, versions and migrates the
ledger schema, unifies durable guard state, and adds adversarial provider
adapter verification plus agent-assisted setup.

### Added

- Effect-commit closure batch for Core Protocol Architecture gaps:
  consequential `claim_side_effecting` now treats derived `effect_id` as the
  authoritative dedupe key (cross-request aliasing, no second-row duplicates),
  storage backends expose `resolve_request_id()` / `get_by_effect_id()` with
  backend-specific effect indexes (file sidecar, Redis secondary key,
  Sqlite/Postgres unique index), and verify invariants add
  `check_unique_effect_id_index`. Added deterministic
  `state-machine-exhaustive` verify scenario plus `sdk/docs/spec/effect_state.tla`
  and `sdk/docs/spec/README.md` formal-model notes.
- Pluggable operator-release authentication through `OperatorAuthorizer`, with
  a small `StaticTokenOperatorAuthorizer` for current deployments. The
  operator identity is now checked instead of treating `--by` as authority;
  enterprise identity and two-person approval remain host workflow concerns.
- Explicit ledger envelope versions, v1-to-v2 upgrade readers, safe migration
  planning and application for file/SQLite/Redis/Postgres, conflict refusal,
  rollback guidance, and Doctor checks for pending or unsupported schemas.
- Unified durable guard state through one namespaced atomic `state_backend`
  shared by loop, scope, completion, state-flush, and audit-receipt storage.
  `mycelium state migrate --plan|--apply` copies legacy per-feature records
  without deleting the source, and Doctor checks backend suitability for the
  declared deployment topology.
- Provider-adapter conformance tooling and CLI commands. `mycelium providers
  verify gmail` exercises lag, ambiguity, duplicate matches, malformed
  handles, false `NOT_EXECUTED`, and forbidden writes, then emits a signed,
  source-bound adapter report; `providers verify-report` verifies it.
- A repository-local `$mycelium-setup` coding-agent skill that inventories
  application tools, classifies their effects, wires `mycelium.yaml`, and runs
  Doctor and Verify while leaving secrets and host-owned identity decisions to
  the developer.

### Changed

- For `keyed_mutate` tools that declare `provider_idempotency_key_param`,
  Mycelium now defaults to propagating `effect_id` as the provider key when the
  kwarg is omitted, persists the injected key on first attempt, and reuses it
  for retries (explicit user-provided keys still win and are enforced). Setting
  `propagate_effect_id_as_provider_key: true` without a declared
  `provider_idempotency_key_param` is now rejected at binding/config load time.
- Stateful guards with no per-feature `storage` setting automatically inherit
  the top-level `state_backend`. Explicit per-feature storage remains available
  for compatibility and rollback.

## 1.34.0 (2026-08-22)

MINOR: one coherent effect-commit and side-effect-safety batch. PyPI 1.32.0
was the last published package: this release therefore includes the complete
v1.33.0 guardrail batch (secret-in-args, destination policy, destructive
confirm, authority-window expiry, and use-time currency) plus all post-v1.33.0
effect-commit work described below.

### Added

- Unified durable WAL intent (`EffectState`) completes the effect-commit
  protocol. New `mycelium.transition.EffectState` (INTENDED / ATTEMPTING /
  COMMITTED / ABORTED / UNKNOWN) plus `resolve_effect_state(entry)` collapses
  the legacy `terminal_outcome` + `effect_phase` + `decision` columns into a
  single state for a ledger row; `LedgerEntry` now persists `effect_id` and
  `schema_version`, and `EffectState` / `resolve_effect_state` are exported
  from the package root. A row parked UNKNOWN (decision present, protocol
  required) stays reconcilable by the durable `decision` gate, which no longer
  consults `resolve_effect_state` for the attempting check. `mycelium.verify`
  gains three new invariant checks asserting the same at-most-one-COMMITTED
  guarantee on the unified EffectState view — `committed_effect_ids_by_state`,
  `check_at_most_one_committed_effect_state`, `check_effect_state_consistency`
  — and the `simulation` verify scenario now asserts the EffectState invariant
  alongside the terminal_outcome one on every crash phase and the two-worker
  fence takeover. Storage CAS kwarg naming now uses
  `expected_effect_state` (renamed from `expected_effect_phase`, same
  EffectState-string semantics), and `profile: production` now defaults
  `action_ledger.unclassified_policy` to `strict` when omitted
  (explicit `warn` remains honored).

- Deterministic simulation proof (effect-commit Change 4). New
  `mycelium.verify.invariants` module (`InvariantViolation`,
  `committed_effect_ids`, `check_at_most_one_committed`,
  `check_provider_mapping`) enforcing the at-most-one-COMMITTED invariant:
  for every effect id at most one `COMPLETED` ledger row may exist, and every
  provider effect must map to at most one COMMITTED row (a provider effect with
  no COMMITTED row is a limitation/warning, never a violation). New
  `simulation` verify scenario (registered in `SCENARIO_ORDER`; multiprocess
  backends only, SKIP on memory): a crash sweep crashes a synthetic worker at
  every boundary phase (`after_claim` / `after_body_start` / `after_boundary` /
  `after_effect`) against a shared durable backend and re-checks the invariant
  against the reconstructed ledger, plus a two-worker fence-takeover proof where
  a lease-expired entry is reclaimed by worker B (fence N+1) and worker A's
  stale-fence `complete()` is rejected by the fencing CAS while B completes
  alone. Scoped to the scenario's own request ids so an `--scenario all` run
  stays independent.

- Tool capability typing (effect-commit Change 3). New `ToolCapability`
  probeability axis (`IDEMPOTENT` / `QUERYABLE` / `BLIND`), orthogonal to
  `SideEffectClass` (idempotency): capability answers "can an in-flight effect's
  outcome be looked up afterward?" `resolve_capability(side_effect_class,
  explicit=None, *, has_reconciler, has_provider_key)` derives a conservative
  default (READ / IDEMPOTENT_MUTATE -> IDEMPOTENT; KEYED_MUTATE -> QUERYABLE with
  a provider key or reconciler, else BLIND; NON_IDEMPOTENT_MUTATE -> QUERYABLE
  only with a reconciler, else BLIND; IRREVERSIBLE -> BLIND, never loosened). An
  explicit declaration may tighten to BLIND freely but may only loosen when the
  class + mechanism supports it — otherwise it raises
  `ToolCapabilityDeclarationError` (declaration honesty). `ToolTransitionBinding`
  gains a `capability` field (`for_tool(capability=...)`; default derived from
  class) and YAML tool bindings accept a `capability:` key (missing -> derived).
  Enforcement: a BLIND tool whose entry is ambiguous (UNKNOWN /
  FAILED_AFTER_EFFECT / maybe_crossed / crossed) never auto-redispatches,
  reclaims-for-retry, or opts into same-key UNKNOWN retry — even with a valid
  provider idempotency key + TTL (BLIND declaration wins); it parks for operator
  reconciliation (`release` still resolves it). A QUERYABLE tool with no probe
  mechanism (no reconciler, no provider key) fails closed to the same parking
  behaviour with a warning. Reconciler presence on the `ActionLedger` is the
  concrete "queryable" mechanism: a bound `Reconciler` lets recovery resolve
  ATTEMPTING -> COMMITTED/ABORTED by probing. Clean FAILED_BEFORE_EFFECT /
  not_crossed retries and IDEMPOTENT tools keep existing behaviour. New public
  exports: `ToolCapability`, `ToolCapabilityDeclarationError`,
  `resolve_capability`.

- Single atomic decision point (effect-commit Change 2). Policy checks are
  evaluated as pure predicates over an `(intent, snapshot)` pair at one
  enforcement point and the resulting `Decision` is recorded atomically with
  the `INTENDED -> ATTEMPTING` transition, under the same fenced compare-and-swap
  that guards every in-flight mutation. New `mycelium.decision` module
  (`Decision`, `DecisionEngine`, `DecisionIntent`, `DecisionSnapshot`,
  `PredicateVerdict`, `build_snapshot`) with a `register_decision_predicate`
  hook so hosts can add checks at the single point. Authority + use-time
  currency are wired as built-in predicates (existing standalone enforcement
  unchanged). `LedgerEntry` gains a durable `decision` field (serde
  backward-compatible; missing -> `None`). A stale-fence worker cannot record a
  decision and therefore cannot smuggle in an effect the current-fence decision
  would deny.

- Kleppmann fencing tokens (effect-commit Change 1). Every successful claim
  atomically increments the durable `LedgerEntry.fence`, and every later
  mutation made for that claim is guarded by the same token. Completion,
  failure, lease renewal, heartbeat, boundary, provider-reference, decision,
  reconciliation, and operator-resolution writes now use fenced storage CAS;
  a worker whose lease was taken over cannot mutate the winning entry even if
  it resumes with its old owner metadata. Legacy rows without a fence load as
  fence zero and receive a current token when claimed.

### Changed

- Budget accounting now warns when `record_usage(steps=...)` is combined with
  the step meter that `check()` and `@budget_guard` enable by default, preventing
  silent double metering. `get_state()` now matches `remaining_budget()` by
  resolving the active execution scope when no key is passed, and the runway
  contract explicitly distinguishes a missing scope from a hard-blocked run.

## 1.33.0 (2026-08-15)

MINOR: side-effect guardrails batch for unsafe / irreversible writes.
Secret-in-args, destination policy, destructive confirm, authority-window
expiry, and use-time currency ship together so decide-time authority
cannot authorize a stale execute-time side effect.

### Community contributions

- **Contributor acknowledgement (added retroactively):** Thanks to
  [@Mr-Neutr0n](https://github.com/Mr-Neutr0n) for improving release safety,
  framework-support documentation, and allowed-path symlink handling in
  [#88](https://github.com/mycelium-labs/mycelium/pull/88),
  [#89](https://github.com/mycelium-labs/mycelium/pull/89), and
  [#90](https://github.com/mycelium-labs/mycelium/pull/90).

### Added

- **Use-time currency (AF-012):** decide-time facts (refundability, ownership,
  inventory, policy revision, price, …) are bound at authorize and
  revalidated at use, immediately before `mark_maybe_crossed` / provider /
  body. Stale (`age >= max_age_seconds`), changed, missing, false, or
  unverifiable facts raise `UseTimeCurrencyError`; no body, no provider
  call, no `maybe_crossed`. Host `use_time_facts.capture` +
  `register_use_time_validator` only — no prompt scanning. Ordered use
  boundary: authority-window expiry then currency. Short-lived use-boundary
  token avoids double validation between `body_start` and
  `mark_maybe_crossed`. Completed ledger RETURN does not revalidate.
  Configurable via `use_time_currency:`; omitted keeps existing behavior.
  Production requires `missing_policy: error`. Together with
  authority-window expiry, completes the five-item side-effect guardrails
  batch. `mycelium doctor` / `mycelium verify --scenario use-time-currency`.

- **Authority-window expiry:** time-bounded authority (destructive grants and
  future adapters) is validated at authorize and again at use, immediately
  before `mark_maybe_crossed` / provider invocation / tool body side effect.
  `now >= expires_at` hard-blocks with `AuthorityExpiredError`; no body, no
  provider call, no `maybe_crossed` marker, no auto-renew. Skew tolerance
  only narrows validity. Completed ledger RETURN retries do not require
  fresh authority. Configurable via `authority_window:`; omitted config
  keeps timeless paths unchanged, while configured `destructive_confirm:`
  still enforces use-time expiry. `mycelium doctor` reports coverage and
  labels clock sync / host timestamps / provider auth / post-check gaps
  `not_verifiable`. Pairs with AF-012 use-time currency for the full batch
  guarantee. `mycelium verify --scenario authority-window`.

- **Destructive confirm (AF-011):** optional `destructive_confirm:` requires
  a host-issued grant for a specific operation on a specific canonical
  object before a configured destructive tool can claim, execute, or
  cross a side-effect boundary. Tool permission is not object
  authorization. The model cannot create, widen, renew, or approve a
  grant. Dual control is intentionally not implemented — two-person
  approval belongs in the host workflow that calls
  `issue_destructive_grant`. Retries with the same stable `request_id`
  reuse the ledger result and do not consume a second use. A grant
  authorizes an attempt; it does not prove the provider outcome or
  replace provider idempotency. Production rejects memory grant storage
  and, on `deployment.topology: multi_node`, file/sqlite storage.
  Omitted `destructive_confirm:` keeps existing behavior.
  `mycelium doctor` reports coverage and labels host minting / human
  approval / provider outcome `not_verifiable`.
  `mycelium verify --scenario destructive-confirm` proves ungranted
  objects never claim.

- **Entity / destination guard:** optional `entity_guard:` authorizes write
  destinations (email, https URL, host, entity id) before ledger claim.
  Each tool declares which argument is the destination; the host lists
  exact allowed entities or constrained patterns. Destinations are
  canonicalized (lowercase email/host, parsed https URL, normalized id).
  Missing, malformed, dynamic, undeclared, or unapproved destinations
  fail closed. Canonical destinations are bound into the operation
  fingerprint so a retry cannot change recipients on the same
  `request_id`. Evidence records tool, destination class / approved
  entity id, policy version, and decision — never the sensitive payload.
  Allowlists are host-controlled; the model cannot add recipients.
  Omitted `entity_guard:` keeps existing behavior. Production requires
  `missing_policy: error`. `mycelium doctor` reports coverage;
  `mycelium verify --scenario entity-guard` proves unauthorized
  destinations never claim.

- **Secret-in-args (AF-010):** optional `secret_args:` scans tool arguments
  before claim, fingerprinting, receipts, execution, logs, and outcomes.
  Default omitted config keeps existing behavior. `policy: error` raises
  `SecretInArgsError` before any side effect; `redact` may pass a redacted
  copy only when that cannot change required tool semantics, otherwise
  fail-closed; `warn` preserves compatibility in development but sanitizes
  every persisted or emitted representation. Production consequential tools
  require `error`. Pass `secret://…` references instead of credentials;
  applications must `register_secret_resolver` (no default env-var
  resolver). Fail-closed pre-execution blocking is the primary protection;
  redaction is defense-in-depth. Mycelium cannot sanitize logs created
  inside arbitrary application or provider code. `allow_fields` /
  `allow_tools` weaken protection and should be scoped per tool, not
  globally trusted. `mycelium doctor` reports scanning, fail-closed
  production, resolver registration, broad allowlists, and labels host
  logs / third-party providers `not_verifiable`.
  `mycelium verify --scenario secret-in-args` searches generated artifacts
  for synthetic credentials.

## 1.32.0 (2026-08-13)

MINOR: production verification batch. Read-only readiness diagnosis, synthetic
failure-scenario proofs, strict production identity and adapter wiring, and
distributed durable outcome evidence ship together as one deployment-trust
milestone.

### Added

- **`mycelium verify`:** empirical verification of production guarantees
  against the configured storage backend (`--scenario redispatch|contention|
  worker-crash|storage-outage|ambiguous-effect|reconcile|all`, `--json`,
  `--strict`, `--timeout`, `--rounds`, `--workers`, `--keep-artifacts`,
  `--no-connectivity`).
  Runs the same checks as `mycelium doctor`, then exercises synthetic
  failure scenarios only — never application tools, LLMs, or real
  providers. Isolated `mycelium:verify:<uuid>:` namespace; cleanup deletes
  only verifier-owned rows/keys. Exit `0`/`1`/`2`/`3`. Passing Verify is
  strong deployment evidence, not proof of every external system.
- **`mycelium doctor`:** read-only production-safety verification
  (`--config`, `--json`, `--strict`, `--verbose`, `--no-connectivity`).
  Checks profile/tool classification, request identity, durable ledgers,
  run-id guards, completion/budget adapter wiring, outcome emission, and
  optional `deployment.topology`. Never executes tools or LLM calls.
  Exit `0`/`1`/`2`; CI gate:
  `mycelium doctor --config mycelium.yaml --strict --json`.
- **Production request identity:** `action_ledger.request_identity_policy`
  (`derived` development default, `require_explicit` in
  `profile: production`). Consequential tools must pass a host-owned
  `request_id` or declare `request_id_from` before claim or execution.
  `tool_call_id` / `run_id` / `thread_id` are not accepted as business IDs.
  Weaker explicit `derived` under production is rejected.
- **Production outcome emission:** `profile: production` requires
  `outcome_emit:` with durable (non-memory) storage. Emission failure is
  fail-closed and does not replace a tool/provider exception. File storage
  is single-node. Development may still omit outcome emission.
- **Distributed outcome storage:** `outcome_emit.storage` accepts
  `postgres` (recommended multi-node durable backend) and `redis`
  (Redis Streams + event-id dedupe). Production Redis requires an explicit
  `persistence: required` acknowledgement; Mycelium cannot verify the
  server's AOF/durable configuration. File remains single-node. Memory
  stays development-only. `OutcomeRow.event_id` supports idempotent
  retried inserts.


- **Automatic LLM budget enforcement:** when `budget:` is enabled, supported
  LangGraph/LangChain chat models are patched so `invoke` / `ainvoke` /
  `stream` check limits before the provider call and record usage once from
  response metadata. `missing_usage_policy: warn` (default) or `error`.
  Production requires an explicitly selected LLM adapter
  (`integrations.langgraph.enabled` or `register_llm_budget_adapter` —
  merely having LangGraph installed is not enough) and `error` whenever
  token/cost limits are set; `max_usd` needs a registered cost resolver.
  Step/time-only budgets may run without token metadata. Manual
  `check("llm")` / `record_usage` / `register_llm_budget_adapter` remain
  the custom-provider fallback.
- **Automatic CompletionContract terminals:** when `completion:` is enabled,
  supported frameworks (LangGraph `END` via `invoke` / `ainvoke` / `stream`)
  run the checklist after a finished stream that reached END. `stream` /
  `astream` yield intermediate chunks as they arrive and withhold the
  last (terminal) chunk until the check passes. Developers
  configure the YAML checklist only — no `complete_run()` call. Production
  verifies an explicitly selected adapter at startup (`ConfigError` if
  `completion:` is on but `integrations.langgraph.enabled` is absent and
  no `register_terminal_adapter` ran). Development warns and keeps
  `wrap_final_message` / `gate_graph_end` / `complete_run` /
  `register_terminal_adapter` as the custom-runtime fallback. Wiring is
  idempotent.

## 1.31.0 (2026-08-11)

MINOR: production-safe identity and storage. Host-owned `request_id`,
strict run-id / memory-storage policies, and `profile: production` so a
deployment cannot silently skip those guards. Library defaults stay `warn`.

### Added

- **`action_ledger.memory_storage_policy`:** `warn` (default, backward
  compatible) or `error`. Side-effecting tools (`idempotent_mutate`,
  `keyed_mutate`, `non_idempotent_mutate`, `irreversible`) plus
  `storage: memory` now raise `ConfigError` at YAML load when policy is
  `error` — the error names the tool and recommends
  `file/sqlite/redis/postgres`. Reads may keep using memory storage.
  Shipped `mycelium init` templates default the action ledger to SQLite.
- **Host-owned `request_id`:** an optional non-empty `request_id` kwarg is
  the transition identity across retries (`charge-order:{order_id}`). It is
  excluded from the args fingerprint and not forwarded to the tool. Omitted
  `request_id` keeps derived `tool_call_id` identity. Reusing the same id
  with a different tool, scope, or meaningful arguments is fail-closed.
- **`missing_run_id_policy` on `loop_guard` / `scope_guard`:** `warn` (default,
  skip when no run identity, backward compatible) or `error`. Strict mode
  raises `MissingRunIdentityError` before the tool, ledger claim, or external
  side effect when an enabled guard has no stable host `run_id`. Empty and
  whitespace-only values are missing. `thread_id` remains a grouping fallback
  only under `warn`.
- **`profile: production`:** opt-in deployment profile. Library defaults stay
  `warn`. Production applies `memory_storage_policy: error` and
  `missing_run_id_policy: error` on enabled LoopGuard/ScopeGuard. Explicit
  weaker `warn` settings are rejected with `ConfigError` (not silently
  overridden). Disabled guards still do not require `run_id`. Reads may keep
  memory storage.

## 1.30.0 (2026-08-11)

MINOR: supervisor handoff audit glue + async peer-wait DX, LangGraph/Redis
adoption recipe, BudgetGuard soft-warn honesty, and CI that actually runs the
concurrency proofs. Batched so partners can `pip install` once.

### Added

- **Thin handoff identity (audit causation):** `handoff_scope(parent_request_id, handoff_id=…)`
  stamps child ledger claims with `parent_request_id` / `handoff_id` for
  supervisor→subagent audit glue. Does not grant capabilities or change
  at-most-once claim keys. Explicit kwargs override the active scope.
  `list_transitions(parent_request_id=…)` and CLI
  `mycelium transitions list --parent` / `show` expose the link.
- **Async wait helper (LangGraph redispatch DX):**
  `ActionLedger.wait_for_transition` / `wait_for_transition_async` poll until
  a known `request_id` leaves `IN_FLIGHT` without reclaim/reconcile/UNKNOWN
  side effects. Decorator claim paths already poll; this is for custom async
  nodes that want a peer-wait without re-claiming.
- **Copy-paste LangGraph + Redis + crash example**
  (`examples/langgraph_redis_crash/`): adoption recipe — redispatch RETURN with
  signed receipt, then mid-flight kill → `HARD_BLOCK` (body once). No new
  capability; stitches existing LangGraph / Redis / receipt / crash paths.

### Fixed

- **BudgetGuard `warn_at` no longer refuses the step** (Honey): soft warn
  emits `warnings.warn` once per dimension and allows the step. Declared
  ceilings stay honest — `warn_at=0.8` is not an 80% hard cap.
  `max_steps=N` pinned to allow exactly N bodies before hard-block.
- **CI runs the at-most-once concurrency proofs** (#69): Redis + Postgres
  services in `.github/workflows/ci.yml`; install `redis`/`psycopg` extras;
  `MYCELIUM_CI_REQUIRE_REDIS` / `MYCELIUM_CI_REQUIRE_POSTGRES` fail hard if
  backends are missing (no silent skip). File-ledger multiprocess tests no
  longer module-gated behind Redis reachability.
- **Version-line hygiene** (#71): READMEs no longer hardcode current
  `vX.Y.Z` / Docs `&release=` pins; release policy documents batching
  docs into release PRs; CI `version-hygiene` job blocks drive-by
  `pyproject.toml` bumps without a matching CHANGELOG header.

### Changed

- Release checklist skill + `AGENTS.md` / `sdk/docs/RELEASE.md` aligned:
  only the release PR moves the version line; badges track PyPI.

## 1.29.0 (2026-08-09)

MINOR: partner-facing reliability batch — safer reclaim default, Redis
tombstones, budget LLM wire, UNKNOWN key window, and `allowed_paths` harden.
All landed on `main` after v1.28.0; cut once so design partners can `pip install`.

### Added

- **Provider idempotency-key validity window on `UNKNOWN`:** declaring both
  `provider_idempotency_key_param` and `provider_idempotency_key_ttl` unlocks
  same-key retry while the window is still `VALID`. Claim prefers Reconciler /
  operator release first; expired keys stay `HARD_BLOCK` (no double-spend after
  provider dedupe expiry). Without the TTL, `UNKNOWN` remains hard-blocked.
  `BLOCKED` is never auto-retried.
- **Budget LLM auto-wiring:** wrap the model turn once so hosts do not have
  to remember `BudgetGuard.check("llm")` before every call.
  `wrap_llm_callable` / `@budget_llm` (raw loop), `instrument_langgraph_llm`
  (chat model `invoke`/`ainvoke`/`stream`/…), `instrument_crewai_llm`
  (`.call`/`.acall`), and duck-typed `instrument_llm` /
  `MyceliumConfig.instrument_llm`. Best-effort token `record_usage` from
  common usage metadata shapes; no USD invention. Framework glue, not
  provider glue.
- **Redis in-flight tombstones:** every ledger `set` writes a durable
  no-TTL tombstone beside the primary key. If Redis TTL-evicts an
  in-flight entry, `get` / `try_claim_inflight` rehydrate an EXPIRED
  ghost so hard-block / death-signal / reconcile still see the prior
  attempt — no silent "first claim" after key loss.

### Changed

- **YAML `transition.reclaim_requires_death_signal` defaults to `true`**
  (was omit → false). `mycelium init` scaffolds `true`. Direct
  `ActionLedger(...)` constructor default remains `false` for backward
  compat. Set `reclaim_requires_death_signal: false` only if you accept
  lease-expiry-as-death.
- Config Redis `in_flight_ttl` default aligned to **604800** (7d), matching
  `RedisLedgerStorage`.

### Fixed

- **`@bounded` `allowed_paths`:** normalize with `os.path.normpath` +
  `PurePath.relative_to` so `..` / sibling-prefix strings (e.g.
  `/workspace/src/../../etc/passwd`, `/workspace_evil`) cannot bypass the
  scope gate. Lexical only (no symlink resolution).

## 1.28.0 (2026-08-08)

MINOR: fail-closed defaults — `@bounded` can express list/dict args, and
same dispatch ticket + different args is refused unless you explicitly opt
out. Batched as one coherent cut (Honey / langchain#39150).

### Changed

- **`action_ledger.on_args_drift` default is `soft`** (was `off`). Same
  `request_id` / `tool_call_id` with different tool args raises
  `ToolBoundaryError` and the tool body does **not** run. Set
  `on_args_drift: hard` to freeze for a human, or `off` for the old
  "new args = new transition" escape hatch. Pinned by
  `tests/test_args_drift.py::test_default_is_soft_not_off`.
- `mycelium init` template scaffolds `on_args_drift: soft`.
- Provider idempotency-key kwargs are excluded from the args-drift
  fingerprint (same as transition-key derivation) so the dedicated
  provider-key gate still owns key mismatches.

### Added

- **`@bounded` array / object schema types:** `{"type": "array", "items": …}`
  and `{"type": "object"}` (optional `additional_properties`) compile
  recursively; unknown type names raise `SchemaBuildError` at decorate time
  (no silent fallback to `str`). Type-aware `pattern` / `min_length` /
  `max_length`. Honey / langchain#39150 shapes covered in
  `tests/test_schema.py` + `tests/test_tool_boundary.py`.

### Fixed

- **`@bounded` bind failures:** undeclared kwargs / bind `TypeError` →
  `ToolBoundaryError` (`unexpected_kwarg` / `missing_required_field`), not a
  raw Python crash.

## 1.27.0 (2026-08-07)

MINOR: budget enforcement (not AF-010) — run-level cost/time/step ceilings
that refuse the next LLM or tool step. Batched with atomicity fixes that
landed under Unreleased.

### Added

- **Budget guard (not AF-010):** run-level `max_duration` / `max_steps` /
  `max_tokens` / `max_usd` ceilings. YAML `budget:` + `@budget_guard` on tools;
  host `BudgetGuard.check("llm")` before LLM turns; atomic `record_usage` for
  host-reported tokens/USD; soft warn → `LedgerHardBlockError` on the next
  step (no mid-flight kill); `mycelium budget status|release`
  (`clear` / `allow-once` / `abort-run`); storage `update()` on memory / file /
  sqlite / redis / postgres. Remaining budget in status; "runway" is pitch-only.

### Fixed

- **`canonicalize_message_id`:** reject interior whitespace / control chars
  (`UNKNOWN`, no Gmail call) — closes `q=` query-split surface flagged on the
  Shadow lane.
- **`LockedJsonDictFile.save`:** `flush` + `fsync` the temp file (and
  best-effort directory fsync) before `os.replace`.
- **`LoopGuard.check`:** atomic storage `update` so concurrent identical
  dispatches share one streak counter (closes get→set race).
- **`ActionLedger.renew_lease`:** CAS via `try_transition` with owner +
  lease-held preconditions (closes TOCTOU that could clobber a concurrent
  complete/reclaim).

### Changed

- Gmail reconciler docs: consumer Message-ID rewrite constraint (Finding A).

## 1.26.2 (2026-08-06)

Patch: adoption blockers + positioning. `mycelium run` can import packages
from CWD without `PYTHONPATH=.`; `__version__` tracks installed package
metadata. PyPI/README lead with **the reliability layer for AI agents** and
the AF-00N catalog; AF-002 flagship is any tool / any provider prove
run-or-not + at-most-once; Gmail adapter framed as a demo, not the headline.

### Fixed

- **`mycelium run`:** put CWD on `PYTHONPATH` so sitecustomize can import
  project packages (e.g. `my_app.tools`) before the interpreter would have
  prepended CWD — no more `No module named 'my_app'` on first trial.
- **`__version__`:** read from `importlib.metadata` (`mycelium-runtime`)
  instead of a hardcoded string that drifted from `pyproject.toml`.

### Changed

- Root / SDK README, failure-mode catalog, failure & threat model: catalog-first
  product story; demote envelope-field jargon and Gmail-as-flagship framing.
- `pyproject.toml` package description updated for PyPI.
- Release cadence policy documented in `sdk/docs/RELEASE.md`.

## 1.26.1 (2026-08-04)

Patch: Gmail sent-log reconciler Message-ID canonicalization (strip + wrap
bare ids in `<>`) so bracketed / bare / whitespace-padded refs share one
query and completed receipt. Backward compatible.

### Fixed

- **`GmailReconciler`:** canonicalize `external_operation_ref` before the
  `rfc822msgid:` query; completed receipts include `message_id` in canonical
  form. Whitespace-only refs stay `UNKNOWN` without calling Gmail.
- Exported helper: `canonicalize_message_id` (package root + `providers`).
- Tests: bracketed/bare/whitespace forms + existing 0/1/2+/missing matrix.

## 1.26.0 (2026-08-04)

Minor: opt-in args-drift / identity-conflict gate on the action ledger.
Same dispatch ticket (`request_id` / `tool_call_id`) with different tool args
can soft- or hard-block. Default remains off (AF-002 Ring 3; backward
compatible).

### Added

- **`action_ledger.on_args_drift`:** `off` (default) | `soft` | `hard`.
  Compares claim-time `args_fingerprint` to prior ledger entries for the same
  dispatch ticket (same storage key, or same ticket under a different
  transition key), scoped by `run_id` / `thread_id` so other runs stay
  isolated. Soft → `ToolBoundaryError`; hard → `LedgerHardBlockError`.
- Decorator kwarg `on_args_drift=` on `@ledger` / `@ledger_sync`.
- Tests in `tests/test_args_drift.py`. Default-off contract still pinned by
  `test_semantic_identity`.

## 1.25.0 (2026-08-04)

Minor: AF-008 scope-escalation guard — freeze the run tool allowlist and
re-check every step so handoffs cannot silently add tools. Backward
compatible (opt-in YAML / API). Entity/path/output stay on `@bounded`.

### Added

- **Scope guard (AF-008):** run-scoped allowlist freeze. Snapshots
  `registry.allowed` / `tools:` (or explicit `allowed_tools`) at bind /
  first step; later tools outside the freeze raise
  `ToolBoundaryError` (`violation=scope_escalation_tool`) or optional hard
  `LedgerHardBlockError`. Re-bind may only narrow. Wrapper order:
  `@state_authority` → `@scope_guard` → `@loop_guard` → `@ledger` →
  `@bounded` → `@protect`.
- YAML `scope_guard:`, CLI `mycelium scope status|bind`, example
  `sdk/examples/scope_guard_allowlist.py`, tests in
  `tests/test_scope_guard.py`.

## 1.24.0 (2026-08-03)

Minor: SQLite ledger backend — zero-ops single-node durable storage (stdlib
`sqlite3`). Opt-in alternative to Redis/Postgres for local/dev and
single-process agents. Backward compatible.

### Added

- **`SqliteLedgerStorage` / `SqliteTaskLedgerStorage`:** `request_id` + JSON
  payload table (Postgres-shaped), WAL, transactional claim + CAS
  `try_transition`. No Redis-style TTL (leases stay in payload).
- YAML: `storage: sqlite` + `path:` (+ optional `table`) for action and task
  ledgers.
- CLI: `mycelium transitions … --sqlite PATH` (env `MYCELIUM_SQLITE_PATH`).
- Tests: storage unit/CAS/concurrent claim; atomicity + operator-release
  fixtures include `sqlite`.

## 1.23.1 (2026-08-03)

Patch: AF-002 failure-case pack — teachable in-process gate repros. Docs/examples
only; no runtime API changes.

### Added

- **Failure-case pack (AF-002):** five runnable in-process repros for
  `RETURN` / `POLL` / `HARD_BLOCK` (+ `REPAIR` / reconcile) under
  `sdk/examples/failure_cases/` — no Redis/Postgres required. New
  `prove_return_completed()` in `mycelium.proofs.feature_demo`; tests in
  `tests/test_failure_cases.py`.

## 1.23.0 (2026-08-03)

Minor: AF-007 completion contract — refuse terminal output while host-declared
required subtasks are still pending. Backward compatible (opt-in YAML / API).

### Added

- **Completion contract (AF-007):** host-declared `required` / `optional`
  checklist keyed by `run_id` (fallback `thread_id`). Mark each id
  `success` | `failed` | `abandoned` (reason required for abandoned).
  Unmarked required → **refuse** (`CompletionRefusedError`); unmarked optional →
  **warn and allow**. Public vocabulary: allow / allow_with_warnings / refuse
  (not soft/hard).
- APIs: `CompletionContract.mark` / `complete_run`, `wrap_final_message`,
  `gate_graph_end`, LangGraph `completion_gate_end`, YAML `completion:`,
  `config.mark_completion` / `config.complete_run`.
- CLI: `mycelium completion status|mark`.
- Example: `sdk/examples/completion_contract_checklist.py`; tests in
  `tests/test_completion_contract.py`.

## 1.22.0 (2026-08-02)

Minor: state-authority execution gate — refuse tool calls derived from a
superseded `state_ref` before ledger claim. Backward compatible (opt-in YAML /
decorator; claim pass-through fields are additive).

### Added

- **StateAuthority:** pre-claim gate that compares a frozen `state_ref` (passed
  on the tool call) against a host `get_canonical_state_ref` callback.
  Mismatch / missing ref → `ToolBoundaryError` (`violation=state_superseded` /
  `state_ref_missing`) or `LedgerHardBlockError`. Wrapper order:
  `@state_authority` → `@loop_guard` → `@ledger` → `@bounded` → `@protect`.
- YAML `state_authority:` (`canonical_callable`, `require_state_ref`,
  `on_mismatch` / `on_missing`, `tools` / `exclude`); per-tool
  `state_authority: false`.
- Ledger pass-through: optional `decision_id` / `state_ref` stored on
  `LedgerEntry` at claim (audit only; not part of transition-key args
  fingerprint). Bookkeeping keys extended accordingly.
- Proof test: stale checkpoint + new `tool_call_id` is allowed by the ledger
  alone and blocked when the gate is wrapped outside it.

## 1.21.0 (2026-08-01)

Minor: AF-003 loop guard — halt repeated identical tool actions across new
`tool_call_id`s. Backward compatible (opt-in YAML; on by default in
`mycelium init --full` / `--minimal`).

### Added

- **Loop guard (AF-003):** run-scoped consecutive action-hash detector. Soft-blocks
  with `ToolBoundaryError` (`violation=loop_detected`) then hard-blocks the whole
  run with `LedgerHardBlockError` until operator release. Wrapper order:
  `@loop_guard` → `@ledger` → `@bounded` → `@protect`. YAML `loop_guard:`,
  `mycelium loops status|release` (`--verified clear|allow-once|abort-run`),
  file/memory storage, example `sdk/examples/loop_guard_db_search.py`.
- Docs sync: root `README.md` + handbook `docs/index.html` (scope, API, Loop guard
  section, YAML) cover AF-003 alongside the SDK README.

## 1.20.6 (2026-08-01)

Patch: align public version strings with the package; clean changelog headers.
No code change.

### Docs

- PyPI badges (`release=`), "API-stable", and "Current package" banners →
  **v1.20.6** (were still on v1.20.4 after the 1.20.5 release).
- `sdk/docs/FAILURE_AND_THREAT_MODEL.md` version note → v1.20.6.
- Remove stray empty `## Unreleased` headers between version sections.

## 1.20.5 (2026-08-01)

Patch: docs-only sync of published surfaces to the v1.20.4 lineage. No code change.

### Docs

- Version sync: PyPI badges (`release=`) and "API-stable (v…)" lines moved
  from v1.20.1 → v1.20.4 in root `README.md`, `sdk/README.md`, and handbook
  `docs/index.html`; `sdk/README.md` "Current package" banner → v1.20.4
  (adds the webhook event-dedupe recipe); `sdk/docs/FAILURE_AND_THREAT_MODEL.md`
  version note → v1.20.4.
- Handbook (`docs/index.html`): webhook event-dedupe pointer to
  `sdk/examples/webhooks/` beside Manual integration; lede "Latest:" now includes
  the webhook recipe (v1.20.3); boundaries link to the SDK README's
  "What Mycelium does not do".

## 1.20.4 (2026-08-01)

Patch: positioning-only docs — "What Mycelium does not do" boundaries. No code change.

### Docs

- `sdk/README.md`: new **What Mycelium does not do** section beside "What it does" —
  explicit boundaries (approvals/policy UI, hosted observability, on-chain audit
  trails, generic webhook hub, rewind/recovery) + the compose line (use Mycelium
  *under* an approval layer and *beside* a tracer — they don't replace each other).
- Root `README.md`: boundary summary + link to the SDK section.
- Handbook `docs/index.html`: one boundary sentence in the lede.

## 1.20.3 (2026-08-01)

Patch: webhook event-dedupe recipe — docs + runnable examples only (no core
ledger change).

### Docs

- `sdk/README.md`: new **Webhook event dedupe (optional)** section beside the
  manual-claim path — claim inbound provider events on the provider event id
  (Stripe `event.id` / GitHub `X-GitHub-Delivery` / Twilio SID) through the
  same `ActionLedger`; at-least-once delivery + durable claim = at-most-once
  handler side effects. Explicitly an adjacent recipe, not a webhook platform.
- `sdk/examples/webhooks/`: runnable one-pagers (fakes, no credentials) —
  `stripe.md`/`stripe_handler.py`, `github.md`/`github_handler.py`,
  `twilio.md`/`twilio_handler.py`. Each: verify signature → claim on event id →
  SKIP / PROCEED-once / HARD_BLOCK fail-closed.
- Root `README.md`: one-line pointer ("Inbound webhook event ids").
- `sdk/README.md` Manual integration example: dropped the `side_effect()`
  helper (no-op + warning outside `@ledger` bodies); documented that the manual
  path is still fail-closed via the durable claim.

## 1.20.2 (2026-08-01)

Patch: sync root README and handbook with the latest implementation.

### Docs

- Root `README.md`: "What it does (v1.20.x)" heading; added **Gmail sent-log reconciler (v1.19.0)** (Core) and **Resolution telemetry + DTTR (v1.20.0)** (Opt-in) bullets.
- Handbook (`docs/index.html`): Gmail sent-log reconciler in Resolution (match matrix), Manual integration note in Actions, new **Outcome telemetry & DTTR** section (+ sidebar nav link), latest-features lede.

## 1.20.1 (2026-08-01)

Patch: document explicit claim → execute → complete (manual integration) beside
the decorator / YAML wrap path; sync public version badges to 1.20.x.

### Docs

- `sdk/README.md`: new **Manual integration (claim → execute → complete)**
  section — same ledger gates as `@ledger_sync`, for partners who own the tool
  runner (PROCEED/SKIP-style). Prefers wrappers; no YAML switch.
- Root `README.md`: link to that section from the non-YAML adoption path.
- `mycelium init -h` description + post-init tip: prefer wrappers; point at
  the manual-integration docs.

### Chore

- Bump package / badge / handbook version refs to **v1.20.1**.

## 1.20.0 (2026-08-01)

Minor: opt-in resolution telemetry (`OutcomeEmitter`) + a pinned **Duplicate
Tool Transition Rate (DTTR)** metric computed after the fact from flat,
append-only rows — a single number that makes the no-double-execute guarantee
observable in production.

### Feature

- `OutcomeEmitter` + `OutcomeRow` (`sdk/mycelium/outcome_emit.py`): flat,
  warehouse-friendly telemetry rows (one JSON object per NDJSON line) emitted
  only on resolution events — a dispatch resolving to a gate
  (`ALLOW`/`RETURN`/`HARD_BLOCK`/`SOFT_BLOCK`), tool-body
  start/complete/fail, and operator release. Poll ticks never emit. Storage is
  memory or file only (no analytics SaaS deps); emission is fault-tolerant —
  a storage failure is logged and swallowed so telemetry can never break the
  tool path or alter claim/CAS/reconcile semantics.
- Wired through `@ledger` / `@ledger_sync` (new `outcome_emitter=` kwarg) and
  `ActionLedger(outcome_emitter=...)`; `release()` emits a `release` row.
  A body run that follows a consumed `NOT_EXECUTED` verdict (reconciler
  `NOT_EXECUTED` or operator release verified `not_executed`) is tagged
  `authorized_reexec=True` so it is never counted as a silent duplicate.
- `compute_dttr()` / `compute_dttr_from_storage()`: `DTTR =
  silent_duplicates / max(long_running_or_redispatched, 1)`, target 0.
  Silent duplicate = a tool-body execution not authorized by a consumed
  `NOT_EXECUTED`; long-running/redispatched = ≥2 resolution events or a span
  longer than `long_running_after` (default `lease_ttl`).
- YAML `outcome_emit:` section (off by default; `storage: memory|file`,
  `path:`, `long_running_after:`) wired through `MyceliumConfig` +
  `config.apply_tool`, plus a commented stub in the full init template.
- CLI: `mycelium outcomes dttr [--config|--file] [--long-running-after N]
  [--json]`.

### Docs

- New public failure & threat model in `sdk/docs/FAILURE_AND_THREAT_MODEL.md`
  (linked near "What `@ledger` does" in `sdk/README.md` and the root README):
  scope, threat actors, guarantees the transition/ledger core provides and
  deliberately does not, guarantee → test map, residual risks. Docs only.
- `sdk/README.md` "Outcome telemetry & DTTR" section (definition + examples).
- `sdk/docs/FAILURE_AND_THREAT_MODEL.md` residual-risk item: silent duplicates
  are invisible without opt-in telemetry; DTTR is the observability measure.

### Tests

- `tests/test_outcome_emit.py`: 25 tests — row round-trip, NDJSON file
  storage (incl. malformed-line tolerance), fault-tolerant emission, the DTTR
  definitions (clean/authorized/redispatched/long-running/empty), `@ledger` /
  `@ledger_sync` hook points (incl. redispatch returning the cached result
  without a body row, body-failure rows, hard-block resolution rows), operator
  release + reconciler `NOT_EXECUTED` authorization, YAML config wiring, and
  the `mycelium outcomes dttr` CLI.

## 1.19.2 (2026-07-31)

Patch: Phase 4 reliability test suite (real-process multiprocess concurrency,
SIGKILL crash-window, backend outages, property-based transition invariants,
Stripe-shaped payment provider mock) + `hypothesis` dev dep + payment-class
identity docs guidance.

### Docs

- Payment-class identity guidance in `sdk/README.md` (recommended production pattern): mint payment-class transition keys / provider keys from server-authoritative values, not raw client or LLM args; deterministic `provider_key = HMAC-SHA256(server_secret, action_id)` passed through `provider_idempotency_key_param`. Guidance only — no API or key-derivation change.

### Tests

- `tests/test_multiprocess_concurrency.py`: 3 real-`spawn`-process tests (file + Redis) — two workers contending for one transition key charge exactly once; crash-after-claim + `NOT_EXECUTED` reconciler reclaim grants exactly one re-execution; cross-process lease fencing.
- `tests/test_process_kill_crash_window.py`: 4 tests that SIGKILL workers mid-tool-body and assert the crash window never double-executes (with and without a `NOT_EXECUTED` reconciler, across file and Redis storage).
- `tests/test_outage_redis_postgres.py`: 11 fail-closed outage tests — Redis down at claim/complete/failure-recording, Postgres down via a stubbed `_require_psycopg` boundary (no psycopg dependency), mid-reconcile outage never re-executes or fabricates a COMPLETED.
- `tests/test_property_transitions.py`: Hypothesis property test over the single-key state machine (file + fakeredis) — claim/complete/fail/crash/release/reconcile/stuck interleavings uphold `executions <= 1 + not_executed_verdicts`, COMPLETED is terminal with a stable result, and mutators CAS strictly out of IN_FLIGHT. Also asserts key-derivation soundness (identical redispatches dedupe, real args re-key, bookkeeping kwargs are excluded from the args fingerprint).
- `tests/test_payment_provider_mock.py`: 10 tests against a read-only Stripe-shaped `Reconciler` + fake PaymentIntent store — `succeeded`→COMPLETED, `requires_payment_method`/`canceled`/missing→NOT_EXECUTED (exactly one re-charge), `processing`→HARD_BLOCK (never re-charges), operator release unblocks with one charge, reconciler never mutates provider state, and a 25-redispatch storm never double-charges.

## 1.19.1 (2026-07-31)

Patch: `mycelium demo` demo-DX pass + docs alignment (field mapping for
external verifiers, request_id identity semantics).

### CLI

- `mycelium demo` demo-DX pass (design-partner feedback from Akash /
  Thskyshield):
  - ASCII-safe output: em dashes / arrows removed and every print routed
    through an ASCII-safe writer, so the tour no longer crashes on Windows
    consoles (cp1252).
  - New explicit HARD_BLOCK section: an ambiguous mutating transition is
    redispatched and the tour visibly shows `LedgerHardBlockError` + the
    `HARD_BLOCK` gate (the product moment) before the operator-release step.
  - Disambiguated metric labels: "Unguarded executions" (bug) vs "Ledgered
    executions" vs "Tool body executions" so identical counts no longer read
    as the same thing.
  - Single-process story: the default tour now states it runs one process on
    an in-memory ledger and points to `mycelium demo --redis` for the
    two-worker cross-process proof (Redis not required for the default tour).

### Docs

- Field mapping for external verifiers added to `sdk/README.md`:
  `request_id` / transition key = Mycelium dispatch identity;
  `external_operation_ref` = handle for read-only reconcile; provider id
  (Stripe `pi_...`, Gmail Message-ID) = what an independent verifier indexes.
  Terminal state is verifier-useful when the Reconciler queries an
  independent source — the ref is a handle, not proof by itself. No
  integration added.
- Identity semantics documented: transition key compounds scope + tool + args
  + class + policy (not `request_id` alone). Same `request_id` + changed args
  = new transition (intentional). Opt-in identity-conflict rejection mode
  discussed but not shipped. (Mengchheang probe 1 / `dp-identity-conflict`
  closed as decided + documented.)

## 1.19.0 (2026-07-30)

Minor: fail-closed Gmail sent-log reconciler (design-partner patch from Shadow / agent-contracts).

### Features

- New `GmailReconciler(service)` — read-only reconciler that queries the Gmail
  API sent-log by RFC 2822 Message-ID to resolve ambiguous email-send transitions.
  Injected ``service`` is duck-typed (no hard google dep); behavior: missing ref →
  `UNKNOWN`, 1 match → `COMPLETED` + provider receipt, 0 matches → `UNKNOWN`
  (indexing lag, never `NOT_EXECUTED`), 2+ matches → `UNKNOWN`, API error →
  propagate (fail-closed).
- New provider sub-package `mycelium/providers/` — home for first-party
  reconcilers (`GmailReconciler`) with room for more providers.
- Exported from package root: `from mycelium import GmailReconciler`.

### Docs

- `sdk/README.md`: worked example of Gmail sent-log reconciliation under the
  Reconciler section, next to the existing Stripe example.

### Tests

- `tests/test_gmail_reconciler.py`: 6 tests covering missing/empty ref, 0/1/2+
  matches, and API error propagation — all using a fake Gmail API service.

## 1.16.0 (2026-07-27)

Minor: worker-death / stream-loss signal — reclaim requires affirmative death evidence when opted in.

- New `reclaim_requires_death_signal` flag (default `False`) on `ActionLedger` and YAML `transition:` section. When on, EXPIRED entries cannot be reclaimed or released without affirmative death evidence — prevents reclaiming from a worker that is merely paused (GC, storage partition, failing auto-renew).
- Death evidence: `worker_dead_asserted_at` is set (via `mark_worker_dead()` / `mark_worker_dead_for()`), OR `last_heartbeat_at` (or `started_at` fallback) is older than the grace window (`presumed_dead_after`, default `2 × lease_ttl`).
- `mark_worker_dead_for(request_id, by=..., reason=..., override_heartbeat=False)` on ActionLedger: refuses when the entry has a recent heartbeat within the grace window (worker may still be alive). Pass `override_heartbeat=True` to bypass the liveness check when the operator has direct evidence of death (appends `" (heartbeat overridden)"` to the audit reason).
- New CLI: `mycelium transitions mark-dead <request_id> --by ... --reason ... [--override-heartbeat]`. `show` now includes `last_heartbeat_at`, `worker_dead_asserted_by`, `worker_dead_asserted_at`. `list --stuck` hints at `mark-dead` for EXPIRED entries without death evidence.
- Release strengthening: when `reclaim_requires_death_signal` is on, `release()` on EXPIRED entries without death evidence raises `LedgerWorkerAliveError`. When off (default), release proceeds unchanged.
- `presumed_dead_after` on `ActionLedger` and YAML `transition:` section: override the default grace window (seconds since last heartbeat / started_at).
- Redis TTL floor: in-flight keys now expire at `max(in_flight_ttl, lease_ttl * 4)` to prevent premature eviction before the death question can be answered. Default `in_flight_ttl` bumped from 3600 to 604800 (7 days). **Behavior change:** existing Redis deployments that relied on 1-hour in-flight key expiry will now retain in-flight entries for up to 7 days — this is intentional (death evidence must be answerable) but changes Redis memory usage. Override via `in_flight_ttl` on `RedisLedgerStorage` if you need shorter TTLs and accept the trade-off.
- New `LedgerEntry` fields: `last_heartbeat_at`, `worker_dead_asserted_by`, `worker_dead_asserted_at` (old serialized entries load unchanged).
- New exception: `LedgerWorkerAliveError` (subclasses `LedgerError`).
- `reclaim_requires_death_signal` and `presumed_dead_after` wired through `ledger()` / `ledger_sync()` decorators and `config.py` `_ledger_timing_kwargs()`.
- 26 tests in `tests/test_worker_death_signal.py` covering field serialization, `has_worker_death_evidence()`, gate behavior (on/off), `mark_worker_dead` with heartbeat guard and override, release strengthening, `presumed_dead_after` defaults, claim-path gating (read-only RECLAIM, side-effecting ALLOW), heartbeat maintenance on claim/renew, Redis TTL floor, and CLI mark-dead + release round-trip.

## 1.18.2 (2026-07-30)

Patch: regression tests from Mengchheang's public repro (no code changes).

### Tests

- Ported the three probes from `notes/design-partners/mycelium_1_16_0_public_repro.py`
  into `tests/test_mengchheang_public_repro.py`:
  1. **Semantic identity** — same caller `request_id` + changed args produces
     a different transition key (intentional; documented).
  2. **Concurrent NOT_EXECUTED reconcile** — BarrierReconciler forces both
     threads to return NOT_EXECUTED simultaneously; CAS loser polls winner.
  3. **Concurrent expired Redis reclaim** — BarrierClient forces both readers
     to see the same stale value; `_try_reclaim` (WATCH/MULTI) gives at most
     one `"claimed"`.
- Pre-1.18.1 bug baselines preserved in module docstring.

### Docs

- Identity-conflict note added to `sdk/README.md` transition-key docs.

### NOT\_EXECUTED CAS-loss path polls instead of hard-blocking

- `_apply_reconcile_result` CAS loss on NOT\_EXECUTED: re-reads the entry
  and returns the winner's fresh claim to the claim loop, which polls until
  the winner completes. Both reconcilers now see the same completed result
  and the tool runs exactly once (instead of one reconciler hard-blocking).
- `_raise_hard_block`: re-reads the entry before raising. If the entry is
  now `IN_FLIGHT` with a live lease (another thread reclaimed it), returns
  to the claim loop instead of raising. Never `mark_blocked`s an entry
  whose lease is currently held.
- `_reconcile_or_hard_block` / `_reconcile_or_hard_block_async`: simplified
  — no longer asserts unreachable after `_raise_hard_block` since it may
  return an entry for polling.
- Claim loop: all six `_reconcile_or_hard_block` call sites (three sync +
  three async) check for `IN_FLIGHT` return and poll instead of returning
  the in-flight entry as a resolved result.
- Operator-resolution path: `_consume_operator_resolution` passes
  `_cas_race_returns_none=True` so CAS loss on operator release falls
  through to the reconciler path instead of stamping the winner's entry.

### Tests

- `test_concurrent_reconcile_not_executed_race` rewritten: asserts both
  threads get the same completed result and the tool ran exactly once
  (instead of one thread hard-blocking).
- New `test_concurrent_reconcile_not_executed_race_expired_seed`: same
  race but seeded from an EXPIRED lease-expired `IN_FLIGHT` entry with
  not_crossed boundary and external_operation_ref.

## 1.18.1 (2026-07-29)

Patch: unguarded-write races in reconcile NOT\_EXECUTED reset and reclaim
paths. Both could produce a lost-update where two concurrent writers each
believe they own the fresh claim — the external design-partner audit
confirmed neither race has been observed in production, but the contract
is now enforced at the storage layer.

### Reconciler NOT\_EXECUTED reset via CAS

- `_apply_reconcile_result` for `ReconcileStatus.NOT_EXECUTED` now uses
  `_try_transition` (CAS) instead of plain `_set_entry`. The expected-from
  set (`_RECONCILE_NOT_EXECUTED_OUTCOMES`) excludes `IN_FLIGHT` so two
  reconcilers cannot both write a fresh in-flight claim; EXPIRED entries
  are advanced to `BLOCKED` first via `mark_blocked` so the CAS pre-condition
  is unambiguous.

### Reclaim path CAS per backend

- `RedisEntryStorage.try_claim_inflight`: the reclaim path (expired lease /
  failed entry rewrite) now uses WATCH-based CAS via
  `_try_reclaim()`.  The stored entry is re-classified under WATCH and only
  overwritten when it is still reclaimable.  A `WatchError` loop retries
  from the top.
- `InMemoryLedgerStorage`: all storage methods (`get`, `set`,
  `try_claim_inflight`, `try_transition`) are now guarded by a
  `threading.RLock`, making concurrent in-process claims safe.
- `FileLedgerStorage`: methods that cross the ``fcntl`` lock are now also
  guarded by a `threading.Lock` (``flock`` has process-level semantics, so
  two threads in the same process cannot rely on it alone).

### Tests

- 5 new tests in `tests/test_atomicity_contract.py` covering both race
  conditions: per-backend reconcile NOT\_EXECUTED race (memory / file /
  redis) and per-backend reclaim race (InMemory, Redis direct).

## 1.18.0 (2026-07-29)

Minor: atomicity contract for one-shot terminal outcomes — stale workers must not silently overwrite already-resolved transitions, enforced via CAS on all storage backends.

### CAS via `try_transition`

- New `LedgerStorage.try_transition(entry, *, expected_terminal_outcomes, expected_owner) -> bool`: per-backend atomic CAS. Memory (opt-in via override on `InMemoryLedgerStorage`), File (within `LockedJsonDictFile.read_modify_write`), Redis (`pipe.watch()` + `WatchError` retry loop), Postgres (`UPDATE ... WHERE payload->>'terminal_outcome' = ANY(...) RETURNING`).
- `ActionLedger._try_transition()` wrapper invoked by `complete()`, `fail()`, `mark_blocked()`, `mark_unknown()` via private `_expected_from` / `_expected_owner` params.
- Public mutators (`complete`/`fail`/`mark_blocked`/`mark_unknown`) default `_expected_from` to `_IN_FLIGHT_OUTCOMES` — refuse writes when the entry is already terminal.
- Resolution paths (`release`, `_apply_reconcile_result`) pass `_expected_from=_RESOLUTION_ACCEPTED_STORED_OUTCOMES` — can complete from `BLOCKED`, `UNKNOWN`, `FAILED_AFTER_EFFECT`.

### Owner fencing

- The `@ledger` / `@ledger_sync` wrapper captures `_ledger_owner()` and passes it as `_expected_owner` to `complete()` / `_record_failure`.
- A stale worker that held a stale lease cannot overwrite a transition another worker already resolved — mismatch raises `LedgerOutcomeAlreadySetError`.
- `_record_failure` catches `LedgerOutcomeAlreadySetError` and re-raises the original tool exception (never masks).

### Poll loop hardening

- `_poll_side_effecting` / `_poll_side_effecting_async`: no longer call `_raise_hard_block` after `mark_unknown` (which now CAS-checks `_expected_from=`). Directly raise `LedgerHardBlockError`.
- `_raise_hard_block` passes `_expected_from=_IN_FLIGHT_OUTCOMES` on `mark_blocked`.

### New exception

- `LedgerOutcomeAlreadySetError` (subclasses `LedgerError`): raised when a terminal-outcome write is refused by the CAS guard.

### Tests

- 126-parametrized test suite in `tests/test_atomicity_contract.py`: transition matrix (6 outcomes × 5 mutators × 3 backends), release/reconcile resolution paths, stalled-worker E2E (complete and fail), owner fencing, two-thread race (same outcome and interleaved).

## 1.17.0 (2026-07-28)

Minor: provider idempotency-key validity window — expired keys hard-block instead of allowing safe retry.

- New `ProviderKeyValidity` enum (`VALID` / `EXPIRED` / `UNTRACKED`) and `provider_key_validity()` pure helper in `mycelium.transition`, styled after `resolve_lease_validity`.
- New `provider_idempotency_key_ttl` field (seconds, per-tool) on `ToolTransitionBinding` and YAML `tools.<name>.provider_idempotency_key_ttl`. When set, the gate checks whether the elapsed time since the first attempt exceeds the declared window. If so, a same-key `FAILED_BEFORE_EFFECT` retry is hardened to `HARD_BLOCK` (the provider may have purged its deduplication state). Omit for today's unchanged behaviour.
- New `provider_key_first_attempt_at` field on `LedgerEntry` (serialized; pre-upgrade entries fall back to `started_at`). Set at first-claim time; carried forward across reclaim, Reconciler `NOT_EXECUTED` reset, and operator-release consumption.
- `resolve_side_effect_gate()` new optional `now` parameter. The `RETRY_ONLY_WITH_SAME_PROVIDER_IDEMPOTENCY_KEY` + same-key branch now additionally calls `provider_key_validity()` and returns `HARD_BLOCK` when the key has expired.
- `hard_block_message()` new optional `binding` and `now` parameters; when the binding has an expired TTL, the message includes the TTL and key age.
- `ToolTransitionBinding.for_tool()` new `provider_idempotency_key_ttl` parameter.
- `mycelium transitions show` prints `provider_key_first_attempt_at` and its age.
- 21 tests in `tests/test_provider_key_validity.py` covering the helper function, gate behaviour, serialisation round-trip, config wiring, and end-to-end expired-key hard-block.

## 1.16.1 (2026-07-28)

### CI / release automation

- New `release.yml` workflow triggered on push to main: reads version from `sdk/pyproject.toml`, creates annotated tag `v{version}`, extracts changelog section for GitHub Release notes, and publishes to PyPI via the existing publish workflow. Idempotent — merges that don't bump the version release nothing.
- `publish.yml` converted to a reusable workflow (`workflow_call`) so the release automation can trigger it directly, bypassing the `GITHUB_TOKEN` restriction on tag-push-triggered runs.

### Docs / proofs

- Expand `mycelium demo` into a full feature tour: unguarded duplicate, transition envelope, lease auto-renew, REPAIR, provider reconcile, class-aware READ retry, operator release, plus optional `--redis` two-worker proof. New proofs in `mycelium.proofs.feature_demo` and `tests/test_feature_demo.py`.
- `mycelium demo --slow` pauses between lines/sections for screen recording.
- `mycelium demo` uses ANSI colors on a TTY (red = without/bug, green = PASS, yellow = EXECUTING). Set ``NO_COLOR=1`` to disable.

### Fixes

- `_get_entry` / `_set_entry` storage wrappers call `self._storage.get` / `set` (they previously recursed into themselves).

## 1.15.0 (2026-07-27)

Minor: operator release workflow for hard-blocked transitions — a recorded human verification (`completed` / `not_executed`) that lets a stuck side-effecting transition recover instead of raising `LedgerHardBlockError` forever.

### Behavior

- `ActionLedger.release(request_id, verified=..., result=..., by=..., reason=...)`: fail-closed (unknown id, already-`COMPLETED`, `IN_FLIGHT` with a held lease are refused), one-shot (a second release raises `LedgerAlreadyResolvedError`), and never deletes the entry — the resolution is stamped on the durable record so `provider_idempotency_key` enforcement and audit history survive.
- `verified="completed"` marks the transition done with the operator-supplied result; `verified="not_executed"` is consumed by the next claim via the existing `_apply_reconcile_result()` machinery and grants exactly one re-execution (the fresh claim has `operator_resolution=None` but carries the audit fields forward).
- `ActionLedger.list_transitions(stuck=, tool=, outcome=)` for triage: `BLOCKED` / `UNKNOWN` / `FAILED_AFTER_EFFECT` / `EXPIRED`, plus aged `IN_FLIGHT` entries.
- New CLI: `mycelium transitions list [--stuck] [--tool] [--json]`, `show`, `release --verified {completed,not-executed}` — reads storage from the config's `ledger:` sections or direct `--file` / `--redis-url` / `--postgres-dsn` flags (env fallback) for operator machines; never executes tools.
- New durable `LedgerEntry` fields: `operator_resolution`, `resolved_by`, `resolution_reason`, `resolved_at`, `released_from_outcome` (old serialized entries load unchanged).
- New exceptions: `LedgerReleaseRefusedError`, `LedgerAlreadyResolvedError` (both subclass `LedgerError`); signed release receipts via `AuditReceiptEmitter.emit_release_receipt` when an emitter is configured.

### Unclassified tool policy

- New `unclassified_policy` parameter on `@ledger` / `@ledger_sync` and `ActionLedger` constructor (`"warn"` default, `"strict"` optional). Tools without a `transition_binding` (unclassified) have unknown side-effect semantics — `warn` emits a one-time `UserWarning` on failed retry (legacy behavior); `strict` routes through `claim_side_effecting` with a conservative binding (`non_idempotent_mutate`) so failed retries hard-block instead of re-executing.
- YAML key: `action_ledger.unclassified_policy: warn|strict` (template updated).
- `ActionLedger.__init__` validates the value and raises `ValueError` on unknown policies.

### Storage warnings

- `claim_side_effecting()` and `claim_side_effecting_async()` now call `_warn_if_volatile_side_effect_storage()` — a one-time per-tool `UserWarning` when a side-effecting tool uses `InMemoryLedgerStorage` (claims are not durable across processes).
- `_warn_memory_storage_for_side_effecting()` in `config.py` warns at YAML load time when transition config + memory storage + side-effecting tool are combined.

### Docs / tests

- SDK README gains an operator runbook ("your agent hard-blocked") with the warning that backend access = release authority (`--by` is an audit stamp, not authentication).
- SDK README gains "What happens when storage is down" (fail-closed contract table) and "Unclassified tools" subsections.
- `tests/test_operator_release.py`: per-backend (memory / file / Redis / Postgres) release round-trips, one-shot and lease rules, keyed_mutate provider-key enforcement after release, old-entry deserialization, CLI round-trip.
- `tests/test_fail_closed_storage.py`: fail-closed storage contract, unclassified policy warn/strict, YAML passthrough, decorator parameter forwarding.

## 1.14.0 (2026-07-27)

Minor: auto-renew execution leases while `@ledger` / `@ledger_sync` tool bodies run, so long work does not look `EXPIRED` to redispatched peers.

### Behavior

- While a ledgered tool executes, a daemon heartbeat extends `lease_until` (default interval `lease_ttl / 3`).
- Opt out with `lease_renew_interval=0` (decorator / `ActionLedger` / YAML `transition.lease_renew_interval`).
- Manual `renew_lease()` remains for extra bumps or claim-outside-decorator flows.

### Docs / version

- Root README, SDK README (PyPI long description), handbook, `docs/llms.txt`, and `mycelium init` template note lease auto-renew / `lease_renew_interval`.
- Bust version banners to v1.14.0.

## 1.13.4 (2026-07-22)

Docs/proof patch: real two-worker Redis Cloud-style #7417 redispatch proof. No new resolution policy.

### Proof

- Add `prove_two_worker_redis_redispatch()` — two OS processes share a real Redis ledger; worker B redispatches while A is `IN_FLIGHT` and must poll (side effect once).
- `mycelium demo --redis` runs that proof after the in-process baseline/guarded steps.
- Pytest: `tests/test_proof_two_worker_redis.py` (skips when Redis unreachable; default `redis://127.0.0.1:6379/15` or `MYCELIUM_TEST_REDIS_URL`).

### Docs

- SDK README + handbook note the Cloud-style Redis proof; AGENTS.md documents the real-Redis test path.
- Bust version banners to v1.13.4.

## 1.13.3 (2026-07-22)

Docs patch: reframe the public #7417 pitch as a **transition envelope**, and document LangGraph Cloud’s ~180s / `BG_JOB_HEARTBEAT` redispatch window. No code changes.

### Docs

- SDK README hero, root README, handbook FAQ / Resolution, and `mycelium demo` copy: pitch class + lease + terminal state + hard-block (not only idempotency key + cached result).
- Note LangGraph Cloud ~180s redispatch aligned with `BG_JOB_HEARTBEAT`; Mycelium lease/poll/hard-block is the operator-side guard.
- Bust version banners to v1.13.3 for PyPI long-description republish.

## 1.13.2 (2026-07-22)

Docs patch: map public transition-sufficiency gate language to Mycelium’s internal gates. No code changes.

### Docs

- Handbook + SDK README: public `ALLOW` / `REPAIR` / `SOFT_BLOCK` / `HARD_BLOCK` (`BLOCK`) ↔ Mycelium `TransitionGate` table (incl. `RETURN` / `POLL` / `RECLAIM`).
- Bust version banners to v1.13.2 so PyPI’s long description picks up the mapping.

## 1.13.1 (2026-07-22)

Docs/packaging patch: sync version banners and PyPI long description after the v1.13.0 `REPAIR` release. No code changes.

### Docs

- Bust shields.io badge cache keys to `release=1.13.1` (root README, SDK README, handbook).
- Root / SDK / handbook version lines: v1.12.0 → v1.13.1; root “What it does” → v1.13.x.
- SDK current-package line mentions `REPAIR` gate.
- Republish so PyPI’s project description picks up the new banners.

## 1.13.0 (2026-07-22)

Minor: first-class ``REPAIR`` resolution gate for incomplete durable transition records. Heal missing context before execute; do not spawn a second side effect.

### REPAIR gate

- Add ``TransitionGate.REPAIR`` — returned when a durable record is missing ``idempotency_key``, has an invalid/missing ``side_effect_boundary``, has an invalid/missing ``terminal_outcome``, or healable status/terminal drift.
- Add ``transition_needs_repair()`` / ``repair_transition_fields()`` and ``ActionLedger.repair_transition()`` — fill safe defaults, then re-resolve (claim loops continue; peers with a held lease still ``POLL``).
- Owner-side ``renew_lease()`` remains the renew half of the taxonomy (extend a live lease so peers keep polling); it does not invent missing terminal state.
- Export ``TransitionGate``, ``transition_needs_repair``, and ``repair_transition_fields`` from the package root.

### Docs

- Document ``REPAIR`` in the resolution-gates table (SDK README + handbook).

## 1.12.0 (2026-07-21)

Minor: add zero-touch, command-based YAML auto-instrumentation while preserving explicit decorator and module instrumentation APIs.

### Command auto-instrumentation

- Add `mycelium run --config mycelium.yaml -- python ...` to apply YAML tool/task guards without editing application functions.
- Add validated, unique `callable: module:function` targets and explicit-name tool/task application helpers.
- Fail closed on missing/non-callable targets, unsupported Python startup flags, and partially wrapped callables; skip fully configured wrappers to prevent double instrumentation.
- Preserve child argv, environment, working directory, signals, and exit status by replacing the launcher process with the target Python interpreter.
- Preserve optional LangGraph `ToolRuntime` identity propagation for command-wrapped tools.

## 1.11.0 (2026-07-21)

Minor: automatically propagate trusted LangGraph runtime identity into Mycelium transition keys, without coupling the core transition model to LangGraph.

### LangGraph integration

- Add the optional `mycelium-runtime[langgraph]` extra and `integrations.langgraph.enabled` YAML setting.
- `@config.apply` adds LangGraph's hidden, trusted `ToolRuntime` parameter to configured ledgered tools and maps `tool_call_id`, thread ID, run ID, and graph node into Mycelium's generic dispatch/execution scope.
- Explicit `request_id`, `tool_call_id`, and scope kwargs continue to override captured framework metadata; direct calls and custom executors keep the existing manual-ID path.
- The default `mycelium init` LangGraph scaffold enables the integration; the full reference template documents it as optional.
- Add real compiled-`ToolNode` conformance tests proving identical redispatches execute the underlying side-effecting tool once, plus config, precedence, scope, and fallback coverage.

## 1.10.1 (2026-07-20)

Docs/packaging patch: fix stale PyPI/README version badge and root README heading. No code changes.

### Docs

- Bust the shields.io PyPI badge cache key (`&release=1.10.1`) — the old `?cacheSeconds=60` URL was Cloudflare-cached at **v1.9.3** for hours after 1.10.0 published.
- Root README: `What it does (v1.9.x)` → `(v1.10.x)`; version banners → v1.10.1.
- Republish so PyPI’s project description picks up the new badge URL and current version line.

## 1.10.0 (2026-07-20)

Minor: make the execution **lease validity window** first-class in resolution, and add lease renew for long-running tools. No change to `transition_key` (lease stays mutable metadata, not identity).

### Lease validity

- Add `LeaseValidity` (`HELD` / `EXPIRED` / `UNBOUNDED`) and `resolve_lease_validity()` — gates already checked time via `resolve_terminal_outcome`; that path now goes through the named validity helper.
- `LedgerEntry.lease_validity()` exposes the same check on durable entries.
- Hard-block messages for `EXPIRED` include `lease_until` for operators.

### Lease renew

- `ActionLedger.renew_lease(request_id)` extends `lease_until` while still `IN_FLIGHT` and not yet expired (rejects completed / already-expired).
- Module helper `renew_lease()` for use inside `@ledger` / `@ledger_sync` tools — keeps peers on `POLL` instead of opening a reclaim path mid-work.

### Docs / packaging

- Document that lease is resolution-first-class (check validity before reclaim/retry), not part of the transition key.
- Bump to v1.10.0.

## 1.9.3 (2026-07-20)

Docs patch: republish so PyPI’s project description matches the current `sdk/README.md`, and document the transition-envelope field stack in the handbook.

### Docs

- Document the six transition-envelope fields in priority order (`side_effect_class` → `spendability` → `side_effect_boundary` → `terminal_outcome` → `external_operation_ref` → `retry_permission`) and the invariant: required fields for a tool class must be supported/recorded before a redispatch is treated as a safe retry.
- Handbook `#envelope` section + root / SDK README pointers (Mycelium branding; no third-party product names).
- Bump version banners to v1.9.3 so the PyPI long description ships with Resolution gates, SOFT_BLOCK, EXPIRED reclaim, and the envelope field stack.

## 1.9.2 (2026-07-20)

Patch: close the remaining gap on **EXPIRED + not_crossed → reclaim only if provable** via `external_operation_ref` reconcile. No new schema or policy concepts.

### EXPIRED reclaim (prove via reconcile)

- Side-effecting poll loops (`_poll_side_effecting` / async) no longer hard-block immediately when a lease expires mid-poll. They return so the outer claim loop can resolve `HARD_BLOCK` through the existing `Reconciler` path.
- Strict classes (`single_use` / payment / etc.) still gate `EXPIRED + not_crossed` as `HARD_BLOCK`; reclaim happens only when an `external_operation_ref` is present and the reconciler returns `NOT_EXECUTED` (unchanged fail-closed rule when ref/reconciler is missing).
- Clearer hard-block error text for stale leases: distinguishes `not_crossed` (reclaim only if reconcile proves `NOT_EXECUTED`) from `maybe_crossed` / `crossed` (effect may have happened).
- Tests: reclaim on `EXPIRED + not_crossed + ref + NOT_EXECUTED`; hard-block without ref; poll-return then reconcile.

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
