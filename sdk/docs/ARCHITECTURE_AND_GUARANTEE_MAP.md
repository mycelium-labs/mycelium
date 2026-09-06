# Mycelium architecture and guarantee map

This document traces the shipped Python runtime from configuration to outcome
evidence. It is deliberately stricter than a feature overview: every guarantee
is conditional on its stated prerequisites and links to both implementation and
tests. It does not claim that installing Mycelium, loading YAML, or passing a
synthetic verification run proves an application's real provider integration.

## Source provenance and review baseline

Validated against the repository at review base commit `2d175f3` (Mycelium `1.38.2`).
The machine-verifiable source manifest is tracked in [architecture_provenance.json](architecture_provenance.json).

### Provenance model and guarantees

To eliminate the impossible Git self-reference problem (a tracked architecture document cannot contain the Git commit SHA that includes the document itself), Mycelium couples an **ancestor review base** with a **cryptographic source file manifest**:

1. **Review Base Ancestry**: The recorded `review_base_commit` (`2d175f3`) is verified to exist in the repository object graph and to be an ancestor of `HEAD`.
2. **Cryptographic Manifest**: [architecture_provenance.json](architecture_provenance.json) records the SHA-256 digests of all 15 canonical Python runtime modules and 27 regression test suites supporting the architecture trace below.

**What this provenance statement guarantees:**
- **Audit Traceability**: Readers and security auditors have an immutable reference to the exact baseline source tree against which architecture guarantees were verified.
- **Automated Drift Detection**: Continuous integration verifies that the review base commit exists as an ancestor and that all linked architecture files remain present and accounted for.
- **Maintainer Update Command**: Maintainers can refresh the provenance baseline with a single deterministic command:
  ```console
  python .github/scripts/update-architecture-provenance.py
  ```

**What this does not prove:**
- A valid provenance record does not guarantee that third-party runtime dependencies (e.g., external Redis or PostgreSQL engines) satisfy their host assumptions (**A**).
- It does not substitute for active synthetic integration tests (exercised via `mycelium verify`).

The companion [failure and threat model](FAILURE_AND_THREAT_MODEL.md) contains the larger failure catalogue and exhaustive guarantee-to-test index.

## Legend

| Mark | Meaning |
|---|---|
| **G** | Runtime guarantee, when all listed prerequisites hold |
| **A** | Host, provider, deployment, or operator assumption |
| **U** | Unsupported boundary; the runtime does not provide this protection |
| **L** | Possible later scope, not a shipped feature or commitment |

## End-to-end map

```mermaid
flowchart LR
    C[1. YAML load and validation] --> W[2. Wrapper composition]
    W --> I[3. Host and effect identity]
    I --> L[4. Atomic ledger claim]
    L --> G{5. Resolution gate}
    G -->|RETURN| O[9. Outcome evidence]
    G -->|POLL| G
    G -->|ALLOW| D[6. Atomic decision: INTENDED to ATTEMPTING]
    G -->|HARD_BLOCK| R[8. Reconcile or operator resolution]
    D -->|denied| X[ABORTED / no body]
    D -->|allowed| B[7. Side-effect boundary and tool body]
    B -->|success| M[COMMITTED / COMPLETED]
    B -->|before effect| F[ABORTED / FAILED_BEFORE_EFFECT]
    B -->|possibly crossed| U[UNKNOWN / FAILED_AFTER_EFFECT]
    U --> R
    R -->|COMPLETED| O
    R -->|NOT_EXECUTED| L
    R -->|UNKNOWN or error| H[Fail-closed hard block]
    X --> O
    F --> O
    M --> O

    DR[Doctor: static, registration, connectivity evidence] -. gates .-> C
    V[Verify: isolated synthetic scenarios] -. exercises .-> L
    V -. exercises .-> R
```

Doctor and Verify are evidence lanes, not part of a business tool execution.
Outcome recording observes the execution path but is not the authoritative
ledger state machine.

## Architecture trace

| Stage | What enters | What the runtime does | What leaves | Evidence |
|---|---|---|---|---|
| 1. Configuration | YAML, environment-backed storage settings, tool declarations | `load_config()` parses and validates a `MyceliumConfig`; invalid or unsupported configuration raises `ConfigError`. | Typed configuration and per-tool `ToolConfig` | [source](../mycelium/config.py#L4256-L4279), [parse test](../tests/test_config.py#L72-L83), [invalid-config tests](../tests/test_config.py#L227-L266) |
| 2. Wrapper composition | Tool callable plus logical tool name | `apply_tool()` builds the inner `protect`/`bounded`/ledger layers, then outer loop, budget, scope, state, use-time, destructive, entity, and secret policy layers. Ledger-backed decision policies are combined at one atomic decision boundary. LangGraph instrumentation is outermost when enabled. | A wrapped sync or async callable | [source](../mycelium/config.py#L687-L950), [wrapper-order test](../tests/test_missing_run_identity.py#L509-L533), [atomic-policy composition test](../tests/test_decision.py#L213-L255) |
| 3. Request and effect identity | Explicit host `request_id`, `request_id_from`, framework dispatch id, active run scope, tool, canonical args, destination, agent and policy version | The request row key is resolved; consequential calls also derive a destination-aware SHA-256 `effect_id`. Provider idempotency-key fields are excluded from the effect fingerprint so key swapping is detected against the same operation. | Physical `request_id` and authoritative consequential `effect_id` | [request-id source](../mycelium/action_ledger.py#L4464-L4525), [effect-id source](../mycelium/transition.py#L1014-L1205), [identity tests](../tests/test_effect_identity.py#L106-L224), [canonical-row test](../tests/test_effect_id_index.py#L71-L87) |
| 4. Ledger claim | Identity, tool classification, call arguments, storage backend | Read-only, classified side-effecting, and legacy unclassified calls take separate claim paths. Classified side effects first resolve the canonical row by `effect_id`, then atomically claim or resolve existing state. | A fenced `LedgerEntry`, cached result, poll, repair, or hard block | [dispatch source](../mycelium/action_ledger.py#L4840-L4888), [claim source](../mycelium/action_ledger.py#L3183-L3412), [entry fields](../mycelium/action_ledger.py#L660-L783), [atomic claim tests](../tests/test_storage_backends.py#L65-L110), [fence tests](../tests/test_atomicity_contract.py#L920-L980) |
| 5. Resolution gate | Existing durable row, lease, boundary, capability, provider key, operator resolution | The state machine chooses `RETURN`, `POLL`, `REPAIR`, `ALLOW`, or `HARD_BLOCK`. Ambiguous `BLIND` effects park. A declared `QUERYABLE` tool without a usable mechanism fails closed. | Safe cached return, waiting, repaired metadata, a claimed attempt, or stopped execution | [claim-loop source](../mycelium/action_ledger.py#L3226-L3408), [capability source](../mycelium/action_ledger.py#L2798-L2866), [gate tests](../tests/test_side_effect_resolution.py), [conformance tests](../tests/test_conformance_tsc007.py) |
| 6. Atomic decision and effect intent | Claimed row, current fence, registered policy facts | The wrapper finalizes policy facts and records the combined decision with the fenced transition from `INTENDED` to `ATTEMPTING`; denial becomes `ABORTED`. Provider-reference, boundary, and completion writes require the allowed attempting phase for protocol rows. | Durable allowed/denied decision and effect intent | [execution source](../mycelium/action_ledger.py#L5137-L5373), [state model](../mycelium/transition.py#L172-L209), [decision test](../tests/test_decision.py#L190-L210), [state-machine test](../tests/test_effect_state_machine.py#L63-L130) |
| 7. Effect boundary and terminal state | Allowed attempt, live owner/fence, optional use-time facts | `side_effect()` durably advances `not_crossed -> maybe_crossed` before the provider call and `-> crossed` after clean return. Failure classification uses the boundary; successful completion commits with the same owner/fence. | `COMMITTED/COMPLETED`, `ABORTED/FAILED_BEFORE_EFFECT`, or ambiguous `UNKNOWN/FAILED_AFTER_EFFECT` | [boundary source](../mycelium/action_ledger.py#L630-L657), [provider-handle source](../mycelium/action_ledger.py#L438-L462), [body/complete source](../mycelium/action_ledger.py#L5375-L5518), [boundary tests](../tests/test_side_effect_boundary.py#L43-L187) |
| 8. Reconciliation | Ambiguous row with optional `external_operation_ref`, read-only reconciler, or operator verdict | `COMPLETED` records provider-confirmed completion without body execution. `NOT_EXECUTED` performs a fenced reset that authorizes one attempt. `UNKNOWN`, missing evidence, or reconciler error hard-blocks. | Settled result, one authorized retry, or durable ambiguity | [contract](../mycelium/reconcile.py#L1-L80), [application source](../mycelium/action_ledger.py#L2694-L2786), [fail-closed source](../mycelium/action_ledger.py#L2868-L3006), [reconcile tests](../tests/test_reconcile.py#L89-L258) |
| 9. Outcome recording | Resolution, body, denial, release, lease, and fence events | `OutcomeEmitter` appends flat event rows. Development `warn` mode logs emitter failure; production `error` mode raises. DTTR derives silent duplicate observations from `body_start` rows and excludes authorized re-execution. | Durable or development telemetry, metric exports, and DTTR | [row/emitter source](../mycelium/outcome_emit.py#L43-L315), [metric projection](../mycelium/outcome_export.py#L100-L171), [event tests](../tests/test_outcome_emit.py#L270-L314), [DTTR tests](../tests/test_outcome_emit.py#L157-L205) |
| 10. Doctor | Configuration and optional connectivity access | Runs registered read-only checks and labels evidence as static, runtime-registration, connectivity, operator-asserted, or not-verifiable. It never executes application tools or LLM calls. | Diagnostic readiness report; no empirical side-effect proof | [engine](../mycelium/doctor/engine.py#L57-L148), [evidence types](../mycelium/doctor/types.py#L17-L44), [non-execution test](../tests/test_doctor.py#L581-L610), [readiness tests](../tests/test_doctor.py) |
| 11. Verify | Doctor-clean configuration and selected scenarios | Creates an isolated namespace and runs registered synthetic scenarios in child processes. Blocking Doctor findings prevent scenarios. File/SQLite evidence is labelled single-node; Redis persistence remains operator-asserted. | Per-scenario empirical evidence and limitations, not provider certification | [engine](../mycelium/verify/engine.py#L199-L330), [scenario registry](../mycelium/verify/registry.py#L13-L28), [evidence type](../mycelium/verify/types.py#L28-L74), [scenario tests](../tests/test_verify.py#L104-L142), [isolation/limitation tests](../tests/test_verify.py#L272-L330) |

## Guarantee map

These guarantees compose only when the protected call actually crosses the
wrapper, a semantically stable identity is used, the configured storage has the
required durability/atomicity for the deployment, and any provider evidence is
truthful.

| ID | Guarantee | Required conditions | Source and tests |
|---|---|---|---|
| G1 | **One initial claim winner.** Concurrent claims for one transition do not both enter the tool body. Peers poll or return the winner's result. | Same canonical effect identity; atomic backend; protected call path | [claim loop](../mycelium/action_ledger.py#L3183-L3412); [file/Redis claim tests](../tests/test_storage_backends.py#L65-L110); [contention Verify test](../tests/test_verify.py#L117-L128) |
| G2 | **Stale workers cannot overwrite the winner.** Every critical claim mutation is conditioned on the current owner/fence. | Backend correctly implements fenced compare-and-set | [entry fence contract](../mycelium/action_ledger.py#L670-L676); [stale-fence tests](../tests/test_atomicity_contract.py#L920-L980); [stale wrapper test](../tests/test_atomicity_contract.py#L476-L514) |
| G3 | **A consequential body starts only after an allowed durable decision.** Denial records `ABORTED` and does not execute the body. | Ledger-backed classified tool; policy wrapper not bypassed | [decision path](../mycelium/action_ledger.py#L5247-L5363); [decision tests](../tests/test_decision.py#L190-L255); [manual mutation guard tests](../tests/test_decision.py#L895-L980) |
| G4 | **Stable consequential redispatch deduplicates by effect identity.** Alternate explicit request ids resolve to one canonical effect row and remain aliases. | Canonical, complete, host-owned identity inputs | [effect row contract](../mycelium/action_ledger.py#L732-L742); [canonical index tests](../tests/test_effect_id_index.py#L71-L87); [destination identity tests](../tests/test_effect_identity.py#L122-L189) |
| G5 | **The effect boundary is monotonic and crash-conservative.** A crash after `maybe_crossed` is ambiguous and cannot be treated as definitely not executed. | Provider call is inside `side_effect()` or equivalent manual boundary writes | [boundary source](../mycelium/action_ledger.py#L630-L657); [boundary tests](../tests/test_side_effect_boundary.py#L43-L187); [restart durability test](../tests/test_storage_backends.py#L491-L522) |
| G6 | **Ambiguous mutation fails closed without proof.** Missing reconciler/ref, `UNKNOWN`, or reconciler failure cannot silently authorize a second blind effect. | Classified mutating tool; strict recovery path | [fail-closed reconciliation](../mycelium/action_ledger.py#L2868-L3006); [negative tests](../tests/test_reconcile.py#L189-L258) |
| G7 | **Provider-confirmed completion returns without re-execution.** | Read-only reconciler truthfully returns `COMPLETED`; current fence wins | [result application](../mycelium/action_ledger.py#L2718-L2735); [test](../tests/test_reconcile.py#L134-L158) |
| G8 | **Provider-confirmed non-execution authorizes at most one new attempt.** Concurrent verdicts are resolved by fenced CAS; losers follow the winner. | Truthful `NOT_EXECUTED`; atomic backend | [reset source](../mycelium/action_ledger.py#L2736-L2785); [single retry test](../tests/test_reconcile.py#L160-L187); [race tests](../tests/test_atomicity_contract.py#L629-L806) |
| G9 | **Critical claim storage failure prevents execution.** Completion/failure write failures do not turn uncertainty into permission to retry. | Storage exceptions are surfaced through a supported backend | [wrapper claim-before-body](../mycelium/action_ledger.py#L5146-L5205); [failure handling](../mycelium/action_ledger.py#L5424-L5478); [outage tests](../tests/test_fail_closed_storage.py) |
| G10 | **Completed redispatch returns cached data without another body run.** | Same identity and readable completed row | [return path](../mycelium/action_ledger.py#L5196-L5205); [outcome-backed test](../tests/test_outcome_emit.py#L280-L293) |
| G11 | **Outcome evidence distinguishes authorized recovery from silent duplicate execution.** | `outcome_emit` enabled; emitter writes succeed; consumers retain rows | [row semantics](../mycelium/outcome_emit.py#L61-L92); [authorized retry test](../tests/test_outcome_emit.py#L433-L466); [DTTR tests](../tests/test_outcome_emit.py#L157-L194) |
| G12 | **Doctor and Verify do not touch real application tools.** Doctor is diagnostic; ordinary Verify uses isolated synthetic operations and refuses unsafe isolation. | Built-in commands and scenarios; no custom code that violates their contracts | [Doctor engine](../mycelium/doctor/engine.py#L90-L148); [Doctor test](../tests/test_doctor.py#L581-L610); [Verify isolation source](../mycelium/verify/engine.py#L296-L330); [isolation tests](../tests/test_verify.py#L287-L330) |

### What “at most once” means here

The invariant is not universal exactly-once delivery. For one stable effect
identity, the allowed body-execution count is:

```text
initial winning attempt + one attempt for each consumed, proven NOT_EXECUTED verdict
```

A `COMPLETED` verdict returns evidence without a new attempt. An `UNKNOWN`
verdict authorizes none. This is tested directly by the reconciliation and
atomicity tests linked under G7 and G8.

## Assumptions

| ID | Assumption | Consequence if false | Evidence |
|---|---|---|---|
| A1 | **The host routes every consequential provider call through the wrapped callable.** | Direct provider calls bypass claims, policy decisions, boundary tracking, and outcomes. | Wrapper construction is explicit in [`apply_tool()`](../mycelium/config.py#L687-L950); the residual risk is documented in the [threat model](FAILURE_AND_THREAT_MODEL.md#f-residual-risks). |
| A2 | **Identity inputs are server-authoritative, canonical, and semantically complete.** | Missing meaningful fields can collapse distinct effects; model-controlled or changing values can split one effect into several keys. | [request-id guidance in code](../mycelium/action_ledger.py#L4473-L4492), [preimage fields](../mycelium/transition.py#L1094-L1135), [identity-change tests](../tests/test_effect_identity.py#L122-L189). |
| A3 | **The selected backend's atomicity and durability match the topology.** | Memory is process-local; file/SQLite are single-node; Redis persistence is not introspected by Mycelium. | [Doctor topology logic](../mycelium/doctor/engine.py#L36-L54), [backend tests and conditional integration coverage](../tests/test_storage_backends.py), [Verify labels](../mycelium/verify/engine.py#L305-L312). |
| A4 | **A reconciler is read-only and conservative, and its provider observation is truthful.** | A false `NOT_EXECUTED` permits one duplicate effect. | The requirement is a protocol contract in [`Reconciler`](../mycelium/reconcile.py#L68-L80); fail-closed behavior is tested in [`test_reconcile.py`](../tests/test_reconcile.py#L189-L258), but credential scopes are outside that test. |
| A5 | **Provider idempotency behavior and dedupe TTL match the declaration.** | A same-key retry may not be safe after the provider's real window expires. | [binding fields](../mycelium/transition.py#L740-L759), [provider-key tests](../tests/test_provider_key_validity.py), [key enforcement tests](../tests/test_provider_idempotency_key.py). |
| A6 | **Clocks are sufficiently aligned for leases and authority windows.** | Lease expiry or authority checks can occur early or late. | Lease code uses wall-clock time in [`renew_lease()`](../mycelium/action_ledger.py#L4001-L4091); time-bound behavior is tested in [`test_lease_validity.py`](../tests/test_lease_validity.py) and [`test_authority_window.py`](../tests/test_authority_window.py). |
| A7 | **Operators and backend writers are authorized by the host deployment.** | A principal with backend write access can stamp resolution or release data. | The ledger accepts an optional [`OperatorAuthorizer`](../mycelium/operator_auth.py); one-shot semantics are tested in [`test_operator_release.py`](../tests/test_operator_release.py), not the host's IAM. |
| A8 | **Outcome storage is retained and queried correctly.** | Runtime safety can still hold while observability and DTTR evidence are incomplete. | Emitter failure policy is explicit in [`OutcomeEmitter`](../mycelium/outcome_emit.py#L210-L259); failure modes and production requirements are tested in [`test_outcome_emit.py`](../tests/test_outcome_emit.py#L115-L140). |

## Unsupported boundaries

| ID | Not guaranteed | Current boundary evidence |
|---|---|---|
| U1 | **Correct agent reasoning, prompt-injection resistance, or business intent.** Mycelium controls declared execution boundaries, not whether the requested action is wise. | The runtime entry point is a tool wrapper, not a planner: [`apply_tool()`](../mycelium/config.py#L687-L950). Optional input/entity guards cover only declared policies; see their negative tests in [`test_entity_guard.py`](../tests/test_entity_guard.py) and [`test_secret_protection.py`](../tests/test_secret_protection.py). |
| U2 | **Automatic interception of direct SDK/database/provider writes.** | Only the returned wrapper establishes `_ActiveTransition` and invokes the ledger path: [`_run_ledgered()`](../mycelium/action_ledger.py#L5137-L5518). |
| U3 | **Provider truth or deployed read-only reconciler credentials.** | `Reconciler` is a Python `Protocol`, not a capability sandbox: [`reconcile.py`](../mycelium/reconcile.py#L68-L80). Provider conformance tests are adversarial synthetic checks, not live IAM proof: [`test_provider_conformance.py`](../tests/test_provider_conformance.py). |
| U4 | **Exactly-once remote side effects across an unknowable crash window.** | `maybe_crossed` deliberately maps uncertainty to a block and reconciliation rather than claiming certainty: [`side_effect()`](../mycelium/action_ledger.py#L630-L649), [`test_exception_inside_side_effect_marks_unknown_and_hard_blocks`](../tests/test_side_effect_boundary.py#L99-L122). |
| U5 | **Authority remaining valid during a remote network call.** Validation occurs immediately before the boundary, but the fact can change afterward. | Use-boundary enforcement is visible in [`_run_ledgered()`](../mycelium/action_ledger.py#L5247-L5363) and tested in [`test_use_time_currency.py`](../tests/test_use_time_currency.py) and [`test_authority_window.py`](../tests/test_authority_window.py). |
| U6 | **Multi-step workflow replay/recovery semantics.** The ledger protects tool transitions and optional task entries, not a Temporal-style workflow engine. | Shipped decorators are tool/task boundary wrappers in [`config.py`](../mycelium/config.py#L687-L1020); the explicit limitation is recorded in the [threat model](FAILURE_AND_THREAT_MODEL.md#d-what-we-do-not-protect). |
| U7 | **A passing Doctor as proof that calls are wrapped.** Doctor inspects detectable configuration/registration/connectivity and never executes tools. | [`run_doctor()`](../mycelium/doctor/engine.py#L90-L148), [`test_doctor_never_executes_tools`](../tests/test_doctor.py#L581-L610). |
| U8 | **A passing ordinary Verify run as production-provider certification.** Scenarios use isolated synthetic operations; file/SQLite are single-node evidence and Redis persistence remains asserted. | [`run_verify()`](../mycelium/verify/engine.py#L199-L330), [`test_memory_skips_multiprocess_and_warns_redispatch`](../tests/test_verify.py#L272-L284), [`test_isolation_refusal_unknown_backend`](../tests/test_verify.py#L317-L330). |
| U9 | **Hosted dashboards or alerting.** Mycelium emits rows/metrics; operating the monitoring system belongs to the host. | Export projection and sinks stop at [`export_rows()`](../mycelium/outcome_export.py#L327-L335); exporter behavior is tested in [`test_outcome_export.py`](../tests/test_outcome_export.py). |
| U10 | **Native non-Python runtime enforcement.** | The distributable runtime and public wrappers are Python (`sdk/pyproject.toml`, [`mycelium`](../mycelium)); cross-language hosts must call a protected Python boundary or implement the protocol independently. |

## Later-scope candidates

These are gaps made visible by the current boundary. They are not promised
roadmap items.

| Candidate | Why it is later scope rather than a current guarantee | Current seam |
|---|---|---|
| L1. Cryptographically scoped, short-lived operator release capabilities | The optional authorizer is host-provided; CLI identity stamps and backend access do not establish universal release authority. | [`OperatorAuthorizer`](../mycelium/operator_auth.py), [`ActionLedger.release()` tests](../tests/test_operator_release.py) |
| L2. Two-person or policy-engine approval for destructive recovery | One-shot release and destructive grants exist, but dual control is not implemented. | [`destructive_confirm.py`](../mycelium/destructive_confirm.py), [`test_destructive_confirm.py`](../tests/test_destructive_confirm.py) |
| L3. Credential-scope attestation for reconcilers | Conformance can challenge behavior but cannot prove a live token is read-only. | [`provider_conformance.py`](../mycelium/provider_conformance.py), [`test_provider_conformance.py`](../tests/test_provider_conformance.py) |
| L4. Cross-language/native runtimes | The current protocol is implemented and packaged in Python. | [`sdk/pyproject.toml`](../pyproject.toml), [`runtime`](../mycelium/runtime/__init__.py) |
| L5. Workflow-level recovery orchestration | Tool/task transition safety is narrower than replaying a complete workflow graph. | [`apply_tool()` / `apply_named_task()`](../mycelium/config.py#L687-L1020) |
| L6. Managed observability UI and alert lifecycle | Outcome rows, DTTR, and exporter sinks exist; storage, dashboards, retention, and paging remain host-owned. | [`outcome_emit.py`](../mycelium/outcome_emit.py), [`outcome_export.py`](../mycelium/outcome_export.py) |

## Evidence interpretation

| Evidence | What a pass supports | What it does not support |
|---|---|---|
| Unit/property tests | The checked implementation invariant under the test's backend and fault model | Deployment wiring, live provider truth, untested interleavings |
| Doctor `PASS` | Configuration and the check's named evidence kind passed | Tool execution, real call-site coverage, provider outcome correctness |
| Verify `PASS` | The selected synthetic scenario passed in its isolated namespace/backend | Application tool safety, real business-provider behavior, production equivalence |
| Cluster attestation | The named multi-worker sandbox sequence and configuration were exercised | All production failures, uncompromised signing keys, or sandbox/production equivalence |
| Outcome rows / DTTR | Recorded execution and resolution events support the computed metric | Events that were never emitted, retained, or routed through Mycelium |

## Review checklist for a concrete deployment

Before translating this map into a deployment claim, verify all of the
following:

1. Every consequential call site uses the final wrapped callable.
2. Each business operation has one stable, server-owned identity across retries.
3. Tool classification and provider capability declarations are truthful.
4. Storage atomicity, durability, namespace, and topology match the deployment.
5. Provider calls use `side_effect()` and record a durable operation reference as
   early as the provider permits.
6. Reconcilers are read-only, conservative under lag/duplicates/errors, and use
   appropriately scoped credentials.
7. Doctor passes with the intended strictness and evidence kinds are reviewed,
   not merely counted.
8. Verify scenarios run on the relevant backend; skips, warnings, and limitations
   remain visible.
9. Production outcome emission is durable and monitored, with DTTR expected at
   `0.0`.
10. Unsupported boundaries above are accepted or mitigated by the host.
