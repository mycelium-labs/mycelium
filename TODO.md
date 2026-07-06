# Mycelium TODO

Backlog items not yet in v1.2. Sourced from community feedback:

- [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417) — [@Tuttotorna](https://github.com/Tuttotorna) ([field split](https://github.com/langchain-ai/langgraph/issues/7417#issuecomment-4859233724), [shipping invariant](https://github.com/langchain-ai/langgraph/issues/7417#issuecomment-4859465734))
- [langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417) — [@Correctover](https://github.com/Correctover) ([cross-framework validation](https://github.com/langchain-ai/langgraph/issues/7417#issuecomment-4861603050))
- [crewAI#5802](https://github.com/crewAIInc/crewAI/issues/5802) — crash-between-claim-and-complete gap ([@azender1](https://github.com/azender1))



## Design principles (ship narrow, prevent unsafe redispatch)

- [ ] **Core invariant** ([@Tuttotorna](https://github.com/langchain-ai/langgraph/issues/7417#issuecomment-4859465734)): read-only duplicate execution is wasteful but recoverable; payment / write / email / **subagent** duplicate execution is unsafe unless **spendability**, **side_effect_boundary**, **terminal_outcome**, and **external receipt state** are all bound
- [ ] **Phased envelope** — do not ship maximal schema at once; make the read-only vs side-effecting split explicit in code and config first
- [ ] **Read-only path (lighter)** — retry / poll / reclaim allowed
- [ ] **Side-effecting / non-idempotent path (stricter)** — **terminal-state reconciliation before any redispatch** (never treat checkpoint retry as a fresh executable action)
- [ ] **Root cause framing** ([@Correctover](https://github.com/langchain-ai/langgraph/issues/7417#issuecomment-4861603050)): frameworks conflate “tool execution started” with “tool execution completed and result persisted” — ledger must separate these states
- [ ] **Idempotency lives outside the agent runtime** — durable claim in Redis/Postgres/file, not in-memory or graph state only (document as architectural requirement)
- [ ] **Pending / unknown handling** — on retry: return cached if committed; **skip or reconcile if still pending** (not blind re-execute; addresses crash-between-claim-and-complete)



## Transition envelope (v1.3+)

Replace minimal ledger entries (`in-flight` / `completed` / `failed`) with a **side-effect transition** model: same transition key → resolve existing state, do not treat redispatch as a fresh executable action.

### Priority fields (implement first)

- [x] `side_effect_class` — per-tool classification: `read_only`, `idempotent_write`, `non_idempotent_write`, `payment`, `email`, `subagent`, `external_api_mutation`, `onchain_action`
- [ ] `spendability` — `multi_use`, `single_use`, `non_replayable`
- [x] `side_effect_boundary` — `not_crossed`, `maybe_crossed`, `crossed`
- [x] `terminal_outcome` — extend beyond v1.2: `IN_FLIGHT`, `COMPLETED`, `FAILED_BEFORE_EFFECT`, `FAILED_AFTER_EFFECT`, `EXPIRED`, `BLOCKED`, `UNKNOWN`
- [ ] `external_operation_ref` — provider tx id, message id, job id, subagent run id, receipt ref
- [x] `retry_permission` — `safe_retry`, `retry_only_with_same_provider_idempotency_key`, `manual_reconciliation_required`, `never_retry_automatically`



### Transition record shape

- [x] Rich `transition_key` — hash of thread/run/node, tool name, canonical args hash, `side_effect_class`, agent id, policy version (not only `tool_call_id`)
- [ ] Envelope fields on ledger entry: `owner`, `lease_until`, `idempotency_key`, `receipt_ref`, `terminal_outcome`
- [ ] YAML / `@ledger` config to declare `side_effect_class` and retry policy per tool



### Resolution rules by class

**Read-only**

- [ ] Duplicate dispatch with same `transition_key` → return / wait / poll existing result (not `LedgerPendingError`)
- [ ] Stale lease (`EXPIRED`) → safe to reclaim
- [ ] `UNKNOWN` terminal → `SOFT_BLOCK` or retry (cost-dependent)

**Payment / non-idempotent side effects**

- [ ] Same key while `IN_FLIGHT` → never execute again; wait / poll existing transition
- [ ] Stale lease + unknown `side_effect_boundary` → `HARD_BLOCK` (no automatic redispatch)
- [ ] Retry only with same provider idempotency key + proof no committed side effect
- [ ] `maybe_crossed` / `crossed` + unknown terminal → manual reconciliation, not redispatch
- [ ] `COMPLETED` → return `receipt_ref`, not re-run side effect
- [ ] `FAILED_BEFORE_EFFECT` → retry may be allowed per `retry_permission`
- [ ] `FAILED_AFTER_EFFECT` → no automatic retry without provider-level reconciliation
- [ ] Stop auto-reclaim on `failed` for non-idempotent classes (v1.2 reclaims failed entries today)



### Gate taxonomy

- [ ] `ALLOW` — execute (no prior transition or policy permits)
- [ ] `REPAIR` — fix context / args before execute
- [ ] `SOFT_BLOCK` — defer or retry (read-only / low-risk unknowns)
- [ ] `HARD_BLOCK` — stop; require manual reconciliation (payment / unknown boundary)



### In-flight handling

- [ ] Poll / wait on valid `IN_FLIGHT` lease instead of raising `LedgerPendingError`
- [ ] Return **skip / pending** (or reconcile) when outcome unknown — do not re-execute side-effecting tools
- [ ] Optional async wait helper for LangGraph redispatch scenarios (#7417)
- [ ] LangGraph Cloud note: ~180s redispatch aligns with `BG_JOB_HEARTBEAT` sweep interval, not a tool timeout — demo/docs should reference this (#7417)



### Docs & validation

- [ ] Document PHI-FORMULA framing: `Required(τ) ⊆ Supported(τ)` — read-only needs lighter support; payment/write needs boundary + receipt + terminal outcome
- [ ] Cross-link #7417 and #5802 as same “retry-induced duplication” / execution-boundary fault class
- [ ] Proof / demo: payment tool with `HARD_BLOCK` on unknown boundary
- [ ] Proof / demo: read-only subagent with poll-on-in-flight (#7417)
- [ ] Proof / demo: crash-after-claim-before-complete → reconcile, not reclaim (crewAI#5802 / SafeAgent PENDING model)



## Currency at use (v1.4+)

**Problem:** a belief can be consistent with its source and fresh enough at fetch, but wrong at the moment something consequential depends on it — with no write required (stale title in a draft, quoted price, status a human acts on). Grounding evals check **consistency**; TTL at ingestion checks **fetch-time freshness**. Neither checks **currency at use**.

**Framing:** two separate axes — **consistency** (matches source) vs **currency** (still true right now). Trigger is **consequence + dependence**, not the presence of a write.

v1.2 `@protect` is fetch-time TTL only; it does not re-verify beliefs when they are relied on.

### Priority

- [ ] `consequence_class` per tool/fact — `informational`, `decision_bearing`, `side_effecting` (parallel to `side_effect_class` on writes)
- [ ] `dependence` — track which cached beliefs an output or tool call depends on (entity keys, tool results, message claims)
- [ ] **Re-verify at use** — before consequential output or action, re-fetch or validate currency of dependent beliefs (not only on next `@protect` cache miss)
- [ ] **Use-time gate** — if currency check fails: `SOFT_BLOCK` (re-fetch), `REPAIR` (refresh context), or escalate — same gate taxonomy as transition envelope
- [ ] Extend `@protect` or add `@currency` — optional hook: `verify_at_use=True` for decision-bearing reads
- [ ] YAML config: declare consequence class per tool; TTL alone insufficient for `decision_bearing`



### What v1.2 does not cover (be explicit in docs)

- [ ] Stale fact surfaced in agent text with no tool write
- [ ] Belief true at cache write, false at email draft / human handoff
- [ ] “Verify before write” only — reads that change decisions without mutation



### Docs & validation

- [ ] Document consistency vs currency axes in handbook
- [ ] Example: contact title changes between CRM fetch and personalized send — re-verify at use
- [ ] Example: quoted price in summary — decision-bearing read, no write



## Run outcomes & aggregate health (v1.5+ / complementary)

**Principle:** per-trace debugging is for root cause, not the detection layer. Production health needs **boring aggregate checks** over run outcomes and state transitions. Spans show call timing and exceptions; they usually cannot show whether a result **moved the task forward**.

Mycelium is guards-first, not a tracing product — but should emit **compact structured outcomes at each decision boundary** so users can feed trend detection (Langfuse, warehouse, etc.).

### Per-run outcome schema

- [ ] `run_terminal_state` — `completed`, `user_abandoned`, `max_steps`, `no_results`, `tool_failed`, `policy_blocked`, `escalated`, `guard_blocked` (HARD_BLOCK / SOFT_BLOCK)
- [ ] **Per-boundary outcome row** — small struct at each guard decision: gate (`ALLOW` / `REPAIR` / `SOFT_BLOCK` / `HARD_BLOCK`), tool/step, `transition_key` or `request_id`, forward_progress hint
- [ ] `consumed_by_later_step` — optional flag: was tool/channel output actually used downstream, not just successfully called?
- [ ] **Wasted planner work** — log branches/steps chosen then discarded (integration-dependent; document pattern)
- [ ] **Drift dimensions** — step count, latency, token use, retry count tagged by prompt / model / version for aggregate queries



### Failure signature clustering (detection, not debug)

- [ ] Document standard failure signatures for trend alerts: empty search, duplicate retrieval, invalid tool args, low-confidence final answer, human correction, duplicate side effect (ledger hit)
- [ ] Export path: **traces for drill-down** + **compact per-run outcome rows for trends** (extend `AuditReceipt` or add `OutcomeEmitter`)



### Scope boundary (be explicit)

- [ ] Mycelium does **not** replace Langfuse / OpenTelemetry dashboards
- [ ] Mycelium **does** log guard-level outcomes guards already know (ledger claim, boundary reject, currency block) in a warehouse-friendly shape
- [ ] Handbook: pair runtime guards with aggregate health checks; don’t use trace UI as sole production alarm



## Production reliability playbook (community patterns)

**Framing:** prototype = “can the chain produce a good answer once?” Production = **controlling everything around that answer**. Mycelium targets the guard/boundary layer; other items are config, observability, or release process.

### Versioning & change control

- [ ] Document: version **prompts, tool schemas, retrieval config, model, temperature, and routing rules together** — if behavior changes, know which version changed
- [ ] Tag guard config (`mycelium.yaml`, ledger storage, TTL, `side_effect_class`) in the same version bundle as prompt/model deploys
- [ ] **Replay set** — small set of real-ish examples covering known failure modes; run before changing prompts/tools/models (not huge; high signal)



### Compact run record (extends v1.5 outcome rows)

- [ ] Per-run row fields: **input class**, selected route, tools called, retries, **final status**, token cost, latency, **failure category**
- [ ] Explicit **failure states** taxonomy: `no_answer`, `wrong_tool`, `invalid_tool_args`, `low_confidence_retrieval`, `timeout`, `policy_block`, `user_abandoned`, `escalation_needed`, `guard_blocked`, `duplicate_side_effect`
- [ ] Align failure categories with gate taxonomy (`ALLOW` / `REPAIR` / `SOFT_BLOCK` / `HARD_BLOCK`)



### Guardrails at boundaries (Mycelium core)

- [ ] **Validate tool args before execution** — `@bounded` (v1.2); extend with consequence / side-effect class
- [ ] **Validate outputs before showing** — output schema on `@bounded`; document pattern for user-facing answers
- [ ] **Fail closed when confidence is missing** — SOFT_BLOCK / HARD_BLOCK when retrieval or currency check has no confidence; don’t ship low-confidence as fact
- [ ] Idempotency + freshness + boundaries at every tool edge, not only on writes



### Nondeterminism & eval discipline

- [ ] Document: treat nondeterminism as expected — compare **distributions over repeated runs**, not one demo run
- [ ] Replay set reports: pass rate + failure category breakdown per version, not binary pass/fail on a single trace



### LangGraph / LangChain integration

- [ ] Make **state transitions observable** first: every node logs clear **input, output, decision reason, terminal status**
- [ ] Mycelium hooks at tool nodes: emit boundary outcome into graph state or side-channel for LangGraph checkpointers
- [ ] Handbook recipe: LangGraph node observability + Mycelium guards on tool functions (complements #7417)
- [ ] Once transitions are explicit, reliability problems (redispatch, stale checkpoint, discarded branches) become easier to debug

1. dont need default langgraph
2. one tool might have multiple functins right, so is tool name and entry id the corrct key for caching

how much is the caching TTL

what id the dev cancelledd because he wants to give a differnt input