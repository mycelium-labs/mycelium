# Mycelium Transition Envelope

## Status and scope

**Status:** frozen as the development-only `v1alpha1` protocol contract. This is
not a production-ready wire contract or deployment profile. The freeze makes
this revision immutable; it does not promote it to beta or stable status.

This document defines a language-neutral protocol boundary for an application that
needs to make one externally observable effect safe across retries, redispatch,
worker failure, and recovery. It is designed around the current Python runtime,
but it deliberately does not expose decorators, Python classes, import paths,
exceptions, positional arguments, or dataclasses as protocol concepts.

The authoritative component is called the **engine**. In the `v1alpha1`
reference implementation, it is the existing Python runtime behind the local
sidecar. An application client is a transport and integration layer, not a
second safety state machine.

This revision does **not** promise distributed transactions, automatic discovery
of provider calls, or exactly-once execution. The narrow guarantee is:

> Under the documented storage, identity, provider-observation, authorization,
and transport assumptions, the engine prevents a protected effect from being
executed again when the protocol says its prior execution may have happened.

Anything outside the protected boundary remains the host's responsibility.

## Current implementation evidence

The contract is grounded in the current repository:

| Concern | Current evidence |
|---|---|
| Effect classes and retry semantics | `sdk/mycelium/transition.py`: `SideEffectClass`, `ToolCapability`, `RetryPermission`, `Spendability` |
| Unified effect states | `transition.py`: `EffectState`; `sdk/docs/spec/effect_state.tla` |
| Durable record | `sdk/mycelium/ledger_model.py`: `LedgerEntry`, schema version 2 |
| Atomic storage and fencing | `sdk/mycelium/ledger_storage.py`, backend implementations under `sdk/mycelium/storage/` |
| Claim and execution orchestration | `sdk/mycelium/ledger_execution.py` and `ActionLedger` facade |
| Identity and argument drift | `sdk/mycelium/ledger_identity.py`, `transition.py`, `entity_guard.py` |
| Provider boundary | `side_effect()` and `record_external_operation()` in the ledger API |
| Reconciliation | `sdk/mycelium/reconcile.py`, `ledger_recovery.py` |
| Policy decision evidence | `sdk/mycelium/decision.py` and fenced `record_decision()` |
| Operator resolution | `ActionLedger.release()`, `operator_auth.py` |
| Outcome evidence | `audit_receipt.py`, `outcome_emit.py`, `outcome_export.py` |
| Deployment evidence | `doctor/` and `verify/` |
| Formal and executable proof aids | `docs/spec/effect_state.tla`, `verify/proof/`, `verify/scenarios/` |
| Language-neutral adapter | `sdk/mycelium/sidecar.py` and its generated OpenAPI 3.1 document |
| External-language clients | `clients/typescript/` and `clients/go/` |

The development-only wire protocol, Python sidecar, and thin TypeScript and Go
clients now exist. Later-scope deployment and trust features remain proposals.

## Audit status

The following distinctions are normative for this draft:

| Label | Meaning |
|---|---|
| **Current** | Observable behavior in the Python runtime and its existing tests. |
| **Proposed** | A future behavior not implemented by the current `v1alpha1` sidecar. |
| **Deliberate change** | Proposed behavior intentionally different from the current Python API and requiring an explicit compatibility/migration decision. |
| **Assumption** | An external condition required for a guarantee, such as truthful host identity or durable shared storage. |
| **Unresolved** | Not sufficiently specified to implement safely. |
| **Later scope** | Explicitly excluded from the first protocol release. |

Two important corrections govern the rest of this document:

1. **Legacy Python identity is not a portable canonical-JSON protocol.**
   `transition.canonical_json()` uses Python JSON serialization with sorted keys,
   compact separators, and `default=str`. `derive_effect_id_for_call()` hashes a
   transition preimage containing `TRANSITION_SCHEMA`, scope, tool, an argument
   fingerprint, destination, side-effect class, `agent_id`, `policy_version`, and
   optionally `dispatch_id`. This is deterministic for the supported Python call
   paths, but `default=str`, Python tuple/list treatment, number rendering, and the
   optional dispatch ID are not a safe cross-language contract. The implemented
   `v1alpha1` sidecar therefore uses the separate `identity-v1` namespace and
   portable canonicalization profile without silently reinterpreting legacy rows.
2. **Legacy Python `effect_id` is an alias of the transition key.**
   The sidecar derives `identity-v1` effect IDs independently of transport
   dispatch identity. Removing `dispatch_id` from that preimage is a deliberate
   protocol change isolated to the new namespace. Existing legacy Python behavior
   remains unchanged, and automatic migration is unsupported.

# 1. Protocol vocabulary and boundaries

## 1.1 Components

* **Application:** the business process and provider integration. It owns business
  identity, provider credentials, provider calls, and truthful reporting of the
  provider boundary unless a future engine-owned adapter is explicitly used.
* **Client:** a small language-specific library. It validates shapes, canonicalizes
  inputs according to a published version, sends protocol messages, and reports
  boundary events. It must not decide whether an effect may be retried.
* **Engine:** the authoritative implementation of identity verification, policy
  decisions, claims, leases, fencing, state transitions, reconciliation acceptance,
  and operator resolution.
* **Storage:** durable state owned by the engine. It must provide the atomic
  compare-and-set semantics required by the engine and must not be treated as a
  cache.
* **Provider adapter/reconciler:** a read-only observer that can determine whether
  a provider operation occurred. It may be hosted by the application or engine,
  but its credentials and behavior must be explicitly trusted.
* **Operator:** an authenticated authority that can resolve an ambiguous effect.

## 1.2 Message categories

The protocol has four categories rather than one overloaded object:

1. **Command:** an application asks the engine to perform a stateful operation.
2. **Reply:** the engine reports the authoritative result or refusal.
3. **Event:** a client or integration reports an observed fact, such as entering the
   provider boundary. Events are accepted only when ownership and fence checks pass.
4. **Stored record:** the durable canonical representation retained by the engine.

A field can be required in a stored record but absent from an initial command. For
example, `owner`, `lease`, and `fence` are engine-created claim information.

## 1.3 Field ownership and conflict rules

The following matrix is the authoritative ownership summary. `log` means ordinary
operational logs; `outcome` means durable audit/outcome evidence. Sensitive fields
may still be retained in protected storage when required for recovery, but should
be represented by references or redacted projections outside that storage.

| Field | Owner | Client may set? | Engine verifies/derives | Mutable? | Identity input? | Log/outcome default | Missing/conflict behavior |
|---|---|---:|---|---|---:|---|---|
| `protocol_version` | Client proposal, engine negotiation | Proposed | Negotiates supported version | No | No | Yes | Reject unsupported version. |
| `message_type` | Sender | Yes, from registry | Validates operation/shape | No | No | Yes | Reject unknown or incompatible type. |
| `message_id` | Client/host | Yes | Uses for message replay handling only | No | No | Limited | Retry with same ID is message-idempotent; never creates a new effect. |
| `agent_id` | Host/configuration, authenticated by engine | Candidate only | Binds to authenticated application identity | No | Proposed identity domain | Redacted/limited | Missing is allowed only for development policies; conflict is authorization failure. |
| `run_id` | Host/framework | Yes | Validates scope policy; correlation only | No | Current Python scope input, `v1alpha1` wire generally no | Limited | Missing when required is rejected or guard is not applicable. |
| `dispatch_id` | Host/framework | Yes | Records it; does not make it business identity | No | Current Python: yes; `v1alpha1` wire: no | Limited | Missing may use an engine/framework policy; different IDs must not split a protected effect. |
| `request_id` | Host business system | Candidate only | Validates explicit/derived identity policy | No | May select the business lookup, not the derived hash | Limited | Missing is rejected when explicit identity is required; conflict is identity error. |
| `tool_id` | Registered engine contract | Candidate only | Resolves registered tool contract | No | Yes | Yes | Unknown tool or conflict is rejected. |
| `execution_scope` / `destination` | Host plus engine policy | Candidate only | Authenticates tenant and validates allowlist | No for an effect | Yes | Redacted/limited | Missing or broadened scope is denied; conflict is identity/policy error. |
| `canonical_input` | Host/client candidate, engine canonical form | Yes | Canonicalizes and validates against contract | No after claim | Yes | Never raw by default | Unsupported value or drift is rejected. |
| `canonicalization_version` | Engine protocol | Advertise only | Selects supported version | No | Yes | Yes | Unsupported version is rejected; never silently falls back. |
| `effect_class` | Registered tool contract | No in an execution request | Engine derives/verifies | No | Yes | Yes | Client attempt to loosen it is rejected. |
| `capability` / `recovery_capability` | Registered engine contract | Candidate metadata only | Engine derives from configured mechanisms | No per attempt | Yes indirectly | Yes | Inconsistent declaration is rejected or tightened. |
| `policy_version` | Engine/configuration | Requested version only | Selects active policy and records it | No per decision | Current Python: yes; proposed: policy metadata, not effect identity unless explicitly decided | Yes | Unknown/incompatible version is rejected. Policy changes do not rewrite old decisions. |
| `effect_id` | Engine/storage | No authoritative setting | Derives and returns it | No | It is the identity | Yes, equality only | Client mismatch is rejected; never replace server value. |
| `provider_idempotency` | Host/provider contract | Candidate key metadata | Verifies same-key and validity rules | Key write-once | Key excluded from identity when configured current behavior | Redact key | Missing is allowed only for classes that do not require it; conflicting key hard-blocks. |
| `claim` | Engine/storage | No | Atomic claim arbitration | State evolves | No | Yes | Return active owner, committed result, or safe refusal. |
| `owner_id` | Engine/authenticated engine | No | Binds caller to lease | On takeover only | No | Limited | Missing on mutation or mismatch returns ownership error. |
| `lease` / heartbeat | Engine clock and current owner | Request TTL/heartbeat only | Server time and policy | Renewable | No | Limited | Expiry does not prove non-execution; invalid renewal is rejected. |
| `fence` | Storage/engine | No | Exact CAS match | New value on takeover | No | Yes | Stale value returns `STALE_FENCE`; stored row is unchanged. |
| `provider_boundary` | Host observation, engine state | Event candidate | Enforces monotonic boundary and owner/fence | Monotonic | No | Yes, sanitized | Missing leaves prior state; downgrade is rejected. |
| `provider_operation_ref` | Provider/application | Yes as evidence | Stores with ownership; reconciler verifies | Write-once/append-only | No | Redact/indirect reference | Untrusted reference cannot itself commit an effect. |
| `effect_state` | Engine/storage | No | State machine/CAS | Controlled transitions | No | Yes | Invalid transition is rejected. |
| `result` | Provider/application candidate, engine record | Only completion caller | Accepts only from valid owner/fence or resolver | Write-once after commit | No | Redacted/reference by policy | Late/stale result cannot overwrite committed data. |
| `failure` | Client observation, engine classification | Sanitized candidate | Classifies using boundary/state | Append-only detail | No | Sanitized only | Raw secrets/arguments are rejected or removed. |
| `decision` | Engine policy engine | Facts may be supplied by host adapter | Evaluates and records atomic verdict | No | No | Yes, sanitized | Denial is recorded before execution authorization. |
| `reconciliation` | Registered read-only reconciler | No arbitrary verdict | Authenticates and applies CAS | One-shot per resolution | No | Evidence reference | `UNKNOWN`, error, or malformed evidence remains blocked. |
| `operator_resolution` | Authenticated operator/authorizer | No ordinary client setting | Verifies authority and one-shot semantics | One-shot | No | Full audit, sensitive | Unauthorized, stale, or repeated resolution is rejected. |
| `outcome` / `receipt_ref` | Engine/emitter | Observation events only | Emits durable projections | Append-only | No | Yes, redacted | Emission failure is visible; it does not prove provider success. |
| `timestamps` | Engine for state, sender for message | Supplemental sender times only | Uses server clock for lease/CAS policy | Append-only | No | Limited | Invalid time is rejected; client time cannot extend a lease. |
| `extensions` | Namespaced extension owner | Yes within namespace | Validates critical extension policy | Append-only | Extension-defined | Extension-defined | Unknown noncritical fields follow version policy; critical unknowns reject. |

This matrix also resolves terminology: `scope` is the protocol projection of the
current `TransitionScope` plus authenticated tenant/destination data, while
`destination` is a separately canonicalized identity/policy projection where the
entity guard supplies one. They must not be conflated.

### Field size and sensitivity limits

Unless a narrower tool contract applies, protocol identifiers (`agent_id`,
`application_id`, `tenant_id`, `business_request_id`, `tool_id`,
`tool_contract_version`, request/dispatch/run IDs, references, and version strings)
are non-empty UTF-8 strings of at most 1024 bytes. The canonical identity preimage
limit is **64 KiB (65536 bytes) after JCS serialization and UTF-8 encoding, before
adding the domain prefix**. This is distinct from the individual input limits in
the schema. The engine must enforce the aggregate limit. Raw canonical input,
provider keys, references, and business identifiers are sensitive by default and
must not appear in ordinary logs; projections use redaction or references.

# 2. Envelope data model

The following fields are grouped by ownership. `Required` means required when the
field's containing structure is used, not required in every message.

## 2.1 Common envelope fields

| Field | Meaning and use | Required / creator | Client choice and verification | Mutability / sensitivity |
|---|---|---|---|---|
| `protocol_version` | Wire and semantic contract version. | Required; client sends, engine negotiates. | Client proposes a supported version; engine accepts or rejects. | Immutable; not sensitive. |
| `message_type` | Command, reply, event, or stored-record operation name. | Required; sender creates. | Client chooses only from the registry; engine validates. | Immutable; not sensitive. |
| `message_id` | Unique transport-message identifier for replay diagnostics. | Required for commands/events; client creates. | Client may generate it; engine uses it for dedupe of the message itself, not effect identity. | Immutable; usually not sensitive. |
| `created_at` | Sender timestamp for audit and expiry checks. | Required; sender creates. | Engine validates format and may compare with policy bounds; server time is authoritative for leases. | Immutable; potentially sensitive operational metadata. |
| `extensions` | Namespaced forward-compatible fields. | Optional; sender creates. | Unknown extensions are ignored or rejected according to capability negotiation. | Append-only in records; sensitivity is extension-defined. |

`message_id` and `effect_id` are intentionally different. Replaying a transport
message must not create a new effect or bypass the effect record.

## 2.2 Identity and authorization fields

| Field | Meaning and use | Required / creator | Client choice and verification | Mutability / sensitivity |
|---|---|---|---|---|
| `agent_id` | Logical agent/application identity. | Required for production profiles; host creates. | Client may transmit it, but the engine authenticates and may replace/reject it. | Immutable per record; sensitive in some deployments. |
| `run_id` | Workflow/run correlation identity. | Optional for standalone effects, required when run-scoped guards apply; host creates. | Engine treats it as correlation and scope input, not proof of authorization. | Immutable; sensitive operational metadata. |
| `dispatch_id` | One framework dispatch/attempt identity. | Optional but recommended; host/framework creates. | Engine records it but does not use it as business effect identity. | Immutable; sensitive operational metadata. |
| `request_id` | Host-owned business request identity. | Required unless the negotiated identity policy explicitly permits derivation. | Client may propose it; engine validates policy and canonical scope. It must not be a random retry identifier. | Immutable canonical row key; may contain sensitive business identifiers. |
| `tool_id` | Stable logical operation name. | Required; host/configuration creates. | Engine verifies it is registered and allowed. | Immutable; generally not sensitive. |
| `execution_scope` | Tenant, entity, destination, or host-owned authorization scope. | Required whenever policy or identity depends on it. | Host creates; engine validates shape and allowlists. Client must not broaden it. | Immutable for identity; often sensitive. |
| `policy_version` | Version of policy facts and interpretation used for the decision. | Required for consequential effects; host/engine configuration creates. | Client may identify a configured policy, but engine selects or verifies the active version. | Immutable per decision; sensitive only if policy names reveal data. |

The engine must distinguish `request_id` from `dispatch_id`. A new dispatch can
refer to the same request and effect.

## 2.3 Input and identity fields

| Field | Meaning and use | Required / creator | Client choice and verification | Mutability / sensitivity |
|---|---|---|---|---|
| `canonical_input` | Structured, identity-relevant operation input after canonicalization. | Required for effect identity; client supplies a candidate. | Engine canonicalizes or verifies the client digest. The engine must not trust a client-provided hash alone. | Immutable after first claim; often sensitive. |
| `identity_exclusions` | Explicit list/versioned policy for fields excluded from identity. | Optional; policy/engine creates. | Client cannot silently exclude fields; engine validates against tool contract. | Immutable; may reveal policy. |
| `canonicalization_version` | Rules used to turn logical input into canonical bytes. | Required when `canonical_input` contributes to identity. | Engine chooses the accepted version; client advertises support. | Immutable; not sensitive. |
| `effect_class` | Read, idempotent mutation, keyed mutation, non-idempotent mutation, or irreversible mutation. | Required for protected effects; tool contract/engine creates. | Client may declare a candidate; engine verifies the registered contract. | Immutable; not sensitive. |
| `capability` | Probeability/retry capability: `idempotent`, `queryable`, or `blind`. | Required for consequential operations; engine/tool contract creates. | Client cannot loosen capability without engine verification. | Immutable; not sensitive. |
| `recovery_capability` | Whether safe retry, same-key retry, provider query, or human resolution is available. | Required in the engine's stored decision; engine derives it from class, capability, and configured mechanisms. | Client may report available mechanisms; engine decides. | Immutable per attempt, except additional evidence may be appended. |
| `effect_id` | Deterministic identity of the logical external effect. | Required in replies/stored records; engine derives. | Client may send a candidate for diagnostics, never authoritative identity. | Immutable; hash is not secret but can leak equality/correlation. |
| `provider_idempotency` | Provider key, parameter name/placement, validity window, and first-use time. | Optional, required for keyed retry or propagation. | Host supplies provider key capability; engine verifies same-key reuse and TTL. The provider key itself may be sensitive. | Key metadata is append-only; key must be write-once. |

The proposed first release should make the engine authoritative for `effect_id`.
Clients may calculate a local preview, but commands must carry either structured
canonical input or a digest that the engine can independently verify.

## 2.4 Claim, lease, and boundary fields

| Field | Meaning and use | Required / creator | Client choice and verification | Mutability / sensitivity |
|---|---|---|---|---|
| `claim` | Claim outcome and claim metadata. | Present in claim replies and records; engine creates. | Client cannot self-authorize a claim. | Claim outcome is immutable; record metadata evolves. |
| `owner_id` | Worker/process identity holding the current lease. | Engine creates on successful claim. | Client presents authenticated caller identity; engine binds it to owner. | Mutable only on valid takeover; sensitive. |
| `lease` | `leased_until`, heartbeat timestamp, TTL policy, and validity. | Engine creates for leased execution. | Client may request a TTL within policy; engine chooses effective values and server time. | Renewable while valid; sensitive operational data. |
| `fence` | Monotonically increasing token for the current owner. | Engine/storage creates on claim/takeover. | Client echoes it; engine verifies exact equality on every mutation. | Monotonic and immutable for an owner; not secret. |
| `provider_boundary` | `not_crossed`, `maybe_crossed`, or `crossed`. | Engine records from client/integration event. | Client reports observation; engine verifies owner/fence and permitted monotonic transition. | Monotonic, except recovery resolves the record; operationally sensitive. |
| `provider_operation_ref` | Provider-generated operation/receipt reference used for lookup. | Application/provider reports it; engine stores. | Engine treats it as untrusted evidence until a configured reconciler verifies it. | Write-once or append-only; often sensitive. |
| `decision` | Atomic policy result and non-sensitive predicate evidence. | Engine creates at the decision point. | Client may provide facts from host adapters; engine evaluates registered policy and stores verdicts. | Immutable per decision; must exclude secrets and raw sensitive input. |

A lease is not an identity input. Renewal changes the lease without changing the
effect identity.

## 2.5 Resolution, result, and audit fields

| Field | Meaning and use | Required / creator | Client choice and verification | Mutability / sensitivity |
|---|---|---|---|---|
| `effect_state` | Unified state: `INTENDED`, `ATTEMPTING`, `COMMITTED`, `ABORTED`, `UNKNOWN`. | Required in replies and stored records. | Engine only. | CAS-protected and monotonic according to the protocol. |
| `terminal_outcome` | Compatibility/detail outcome such as completed, failed-before-effect, failed-after-effect, blocked, or expired. | Optional protocol detail; engine derives from state/evidence. | Client cannot claim a terminal outcome by naming it. | Append-only evidence; engine controls resolution. |
| `result` | Provider/application result returned after committed completion. | Optional; application reports, engine stores after valid completion. | Client supplies candidate result; engine accepts only from current owner/fence or authorized resolver. | Write-once for a committed effect; may contain sensitive business data. |
| `failure` | Sanitized class, message, and boundary context for failure. | Optional; client reports. | Engine sanitizes and classifies; raw credentials/arguments are forbidden. | Append-only; potentially sensitive. |
| `reconciliation` | Read-only verdict: `COMPLETED`, `NOT_EXECUTED`, or `UNKNOWN`, with evidence reference. | Optional until reconciliation; reconciler creates verdict, engine commits it. | Engine authenticates reconciler and validates allowed transition. | One-shot verdict per attempt; sensitive provider data. |
| `operator_resolution` | `completed` or `not_executed`, reason, operator identity, and authorization evidence. | Optional until operator action; operator/engine creates. | Engine authenticates operator and authorizer; client cannot create it. | One-shot; highly sensitive audit data. |
| `outcome` | Event name, timestamps, latency, evidence reference, and export status. | Optional but required by production outcome policy. | Engine emits; client may report observations. | Append-only; may contain operationally sensitive data. |
| `started_at`, `finished_at` | Engine event times. | Engine creates. | Client timestamps are supplemental only. | Append-only; sensitive operational metadata. |
| `receipt_ref` | Reference to a durable audit receipt. | Optional; engine/emitter creates. | Client cannot assert a receipt was durably emitted. | Append-only; sensitive correlation metadata. |

# 3. State-machine contract

## 3.1 States

`effect_state` is the protocol-level state. The current Python record also retains
legacy fields such as `effect_phase`, `status`, and `terminal_outcome` for storage
compatibility. A wire implementation should expose one unified state and may expose
legacy detail in an extension.

* **INTENDED:** a durable effect intent exists and no provider effect is authorized
  yet. It can be claimed or safely abandoned according to policy.
* **ATTEMPTING:** the engine recorded an allowed decision and an owner is/was
  authorized to approach the provider boundary. The provider may not have been
  called, may be in flight, or may have completed.
* **COMMITTED:** the engine has accepted successful completion evidence for the
  logical effect. A duplicate dispatch returns the stored result and does not run
  the provider call.
* **ABORTED:** the effect was denied or failed before the provider boundary. A new
  attempt is allowed only under the configured retry policy.
* **UNKNOWN:** the provider result cannot safely be inferred. Automatic execution
  is prohibited until a reconciler or authorized operator resolves it.

`BLOCKED`, `FAILED_BEFORE_EFFECT`, `FAILED_AFTER_EFFECT`, `EXPIRED`, and
`IN_FLIGHT` are current detail outcomes. They should map to the unified state plus
reason/evidence rather than become independent competing state machines.

## 3.2 Permitted transitions

| From | Operation/evidence | Ownership/fence | Result and execution authorization | Repeatability and errors |
|---|---|---|---|---|
| no record → `INTENDED` | `propose` | Engine creates the row. | No provider execution yet. | Idempotent by `effect_id`; conflicting identity is rejected. |
| `INTENDED` → claimed `INTENDED` | `claim` | Atomic claim; owner and new fence assigned; lease held. | Claim alone does not authorize provider execution. | Concurrent callers get one owner, an active-owner response, or stored result. |
| claimed `INTENDED` → `ATTEMPTING` | `record_decision(allow)` | Current owner and fence; atomic CAS. | `allow=true` authorizes the owner to enter the provider flow. | One decision point. Stale/mismatched fence or already-resolved row is rejected. |
| claimed `INTENDED` → `ABORTED` | `record_decision(deny)` | Current owner and fence; atomic CAS. | Provider execution is forbidden. | One-shot decision. Policy denial is recorded before returning an error. |
| `ATTEMPTING` → `COMMITTED` | `complete` | Current owner and fence; valid allowed decision. | Effect is recorded as completed; future dispatches return result. | One-shot. Duplicate completion is a conflict or stored-result response. |
| `ATTEMPTING` → `ABORTED` | `fail_before_effect` | Current owner and fence; provider boundary definitely not crossed. | A policy-approved retry may execute later. | Repeatable only for a new valid attempt; conflicting writes are rejected. |
| `ATTEMPTING` → `UNKNOWN` | `mark_unknown` or ambiguous failure | Current owner/fence, or a valid recovery path. | No automatic provider execution. | Terminal-until-resolved; duplicate dispatch polls or blocks. |
| claimed row → lease expired | `lease_expiry` by server time | No client authority needed; storage observes expiry. | Does not prove provider non-execution. | Recovery must use class, boundary, worker-death evidence, and reconciliation. |
| `UNKNOWN` → `COMMITTED` | reconciler says definitely completed | Authenticated read-only reconciler; engine CAS. | Stored provider result may be returned; no re-execution. | One-shot and idempotent for the same evidence. |
| `UNKNOWN`/eligible expired → `ABORTED` or new `INTENDED` attempt | reconciler says definitely not executed, or authorized operator says so | Authenticated reconciler/operator and engine CAS; worker-death safeguards apply. | One new execution may be authorized. | One-shot resolution; concurrent resolution loses by CAS. |
| `UNKNOWN` → `UNKNOWN` | reconciler says unknown or fails | Authenticated reconciler; no weakening. | Remains blocked. | Safe to repeat; no execution. |
| any owned mutation → unchanged | stale fence/owner | CAS rejects. | No result/state overwrite. | Safe retry of the protocol message after rereading state. |

A `claim` can renew or take over ownership only when the engine's lease and worker
death rules allow it. Fencing is required even if a lease appears expired: an old
worker can resume after a new worker has acquired a higher fence.

## 3.3 Transport versus execution

Transport delivery is not provider execution. A command can be delivered twice,
be retried after a timeout, or be acknowledged by a client after the process dies.
The engine's durable claim and provider-boundary events are separate facts.

A client must not interpret a successful `claim` reply as permission to retry an
already ambiguous effect. It must inspect the returned state and authorization.
Likewise, an HTTP timeout does not mean that the provider call did not happen.

## 3.4 Connection-loss matrix

| Connection loss point | Durable fact known to engine | Client behavior on reconnect | External-effect interpretation |
|---|---|---|---|
| Before `claim_effect` is committed | No claim, or an unknown commit result | Retry the same message ID and identity; inspect the reply. | No authorization is implied. |
| After claim commit, before reply | Claim may exist with an owner/fence | Inspect/poll by effect ID; do not blindly claim and execute. | No conclusion about provider execution. |
| After authorization, before boundary event | `ATTEMPTING` may be recorded, boundary may be `not_crossed` | Reconnect with the same owner/fence if valid; otherwise poll/recover. | The provider may not have started, but transport loss proves nothing. |
| During provider call | Boundary may be `maybe_crossed` or `crossed`, or no event may have reached engine | Report the strongest truthful boundary evidence; reconcile if uncertain. | Effect may have happened. Never infer non-execution from disconnect. |
| After provider return, before completion commit | Boundary/reference may be stored, result may not be | Retrieve state, then submit fenced completion only if still authorized. | Provider may have completed; reconciliation/provider key prevents unsafe repeat. |
| After completion commit, before reply | `COMMITTED` and result are durable | Retry/poll; return stored result. | No second provider execution. |
| During reconciliation/operator resolution | Resolution CAS may or may not have committed | Retry the same resolution message or inspect state. | Never execute merely because the resolution reply was lost. |

A heartbeat only demonstrates that the owner process reported liveness at a time.
It does not prove provider progress, provider completion, or that the process will
not stop immediately afterward. A worker that keeps heartbeating while making no
progress is not currently detected by an independent progress monitor. The engine
must not infer completion from that condition; a future progress watchdog is later
scope.

# 4. Protocol operations

The operation names below are frozen `v1alpha1` wire names. They correspond to current
Python capabilities but are not a promise of a particular endpoint shape.

## 4.1 Commands and replies

### `derive_effect_identity`

Request: identity fields, tool contract reference, canonical input, requested
policy/canonicalization versions, and optional provider-key metadata.

Reply: canonical `effect_id`, canonical JSON, canonical byte count, and identity
namespace. The shipped route is `POST /v1/identities/derive`.

This operation is read-only. Repeating identical identity returns the same
derived value; only `claim_effect` creates or resolves a ledger record.

### `claim_effect`

Request: the complete identity candidate, validated decision evidence when
available, and an optional lease request. The engine derives and verifies the
effect ID.

Reply: one of the seven frozen dispositions: `EXECUTE`,
`RETURN_STORED_RESULT`, `WAIT_FOR_OWNER`, `RECORD_DECISION`, `UNKNOWN`,
`DENIED`, or `TERMINAL_ABORTED`.

The engine atomically selects the canonical row, checks state and policy, assigns
a higher fence for a valid new owner, and returns owner/lease authority only
with `EXECUTE`. Every other disposition means the client must not call the
provider. The client cannot choose the fence.

### Decision recording inside `claim_effect`

`v1alpha1` has no separate `record_decision` HTTP route. A claim may carry
validated decision facts; the sidecar records them through the authoritative
engine before returning `EXECUTE`.

An allowed decision produces an `ATTEMPTING` projection with `EXECUTE`; a
denial produces `DENIED`/`ABORTED`.

This is the single mutation gate for an allowed consequential attempt. Policy
facts must be sanitized and durably recorded atomically with the state change.

### `renew_lease`

Request: effect ID, owner, fence, requested extension, heartbeat metadata.

Reply: updated lease and same fence, or `STALE_FENCE`, `NOT_OWNER`, or
`LEASE_EXPIRED`.

Renewal is idempotent for the current owner/fence within the server's policy.
It never changes `effect_id` or state.

### `record_boundary`

Request: effect ID, owner, fence, boundary event (`not_crossed`, `maybe_crossed`,
`crossed`), optional provider operation reference.

Reply: updated boundary and record.

Boundary is monotonic. A client cannot downgrade `crossed` to `not_crossed`.
A provider reference is evidence for reconciliation, not proof of completion.

### `complete_effect`

Request: effect ID, owner, fence, result, optional provider reference, and
completion evidence.

Reply: `COMMITTED` with stored result, or an error.

Only a current owner and fence with a valid allowed decision may complete. A
stale worker cannot overwrite a committed result.

### `fail_effect`

Request: effect ID, owner, fence, failure classification, boundary evidence, and
sanitized error information.

Reply: `ABORTED` when definitely before effect, otherwise `UNKNOWN` or a detail
state selected by the engine.

The client may report what it observed, but the engine selects the safe state.
A failure after or near the provider boundary must not be presented as safe
pre-effect failure merely because the transport failed.

### `get_effect`

Request: effect ID.

Reply: authoritative record projection, current lease validity, state, result if
available, and current ownership/fence information. The shipped route is
`GET /v1/effects/{effect_id}`; clients poll by repeating this read.

Polling is read-only. It must never claim or execute the provider effect.

### `reconcile_effect`

Request: effect ID plus its complete identity candidate. The sidecar asks the
authoritative engine to resolve the existing record with its configured
reconciler.

Reply: authoritative state with
`reconciliation: "authoritative-engine-result"`, or
`RECONCILIATION_UNAVAILABLE`.

`COMPLETED` never executes the provider call. `NOT_EXECUTED` permits at most one
new claim subject to CAS and worker-death rules. `UNKNOWN`, malformed evidence,
provider errors, and timeouts remain blocked.

### Later operations outside `v1alpha1`

`request_operator_resolution`, `apply_operator_resolution`, `emit_outcome`, and
`get_outcomes` describe future protocol work. They are not routes in the frozen
development profile. Operator authorization and outcome storage remain
engine/deployment responsibilities.

## 4.2 Expected protocol errors

Use stable machine-readable codes with human-readable detail:

`UNSUPPORTED_PROTOCOL`, `INVALID_ENVELOPE`, `IDENTITY_REQUIRED`,
`IDENTITY_CONFLICT`, `ARGUMENT_DRIFT`, `POLICY_DENIED`, `NOT_FOUND`,
`ACTIVE_OWNER`, `STALE_FENCE`, `NOT_OWNER`, `LEASE_EXPIRED`, `ALREADY_COMMITTED`,
`UNKNOWN_REQUIRES_RESOLUTION`, `RECONCILIATION_UNKNOWN`, `INVALID_VERDICT`,
`OPERATOR_UNAUTHORIZED`, `STORAGE_UNAVAILABLE`, `PROVIDER_REFERENCE_REQUIRED`,
and `CAPABILITY_MISMATCH`.

Errors are protocol replies, not language-specific exceptions. A TypeScript client
may map them to ergonomic error classes.

## 4.3 Error contract

Every error reply has this shape:

```json
{
  "error": {
    "code": "STALE_FENCE",
    "message": "mutation rejected by current fence",
    "retryable": false,
    "caller_action_required": false,
    "state_may_have_changed": false,
    "effect_may_have_happened": true,
    "effect_id": "...",
    "details": {"current_state": "UNKNOWN"}
  }
}
```

`retryable` describes retrying the same protocol message, not retrying the external
effect. `state_may_have_changed` is true for ambiguous transport outcomes. The
engine must not expose raw exception names, secrets, or unredacted provider
payloads. `effect_may_have_happened` is deliberately conservative and may be true
when the current state is not yet known. Stable codes and their retry/action
semantics are part of the future versioned error registry.

| Code | Same-message retry | Caller action | Effect may have happened |
|---|---|---|---|
| `INVALID_REQUEST`, `UNSUPPORTED_PROTOCOL`, `CAPABILITY_MISMATCH` | No until corrected | Correct client/negotiation | No authorization implied |
| `IDENTITY_CONFLICT`, `ARGUMENT_DRIFT` | No with changed identity | Reconcile business identity; do not alter inputs silently | Existing effect may have happened |
| `POLICY_DENIED` | No without a new policy decision | Change authorized inputs or policy | No, for this denied attempt |
| `ACTIVE_OWNER` | Poll/retry inspection | Wait for owner or recovery | Unknown until record resolves |
| `STALE_FENCE`, `NOT_OWNER`, `LEASE_LOST` | Only after rereading state | Stop stale work; never replay provider call blindly | Yes/unknown |
| `INVALID_TRANSITION`, `ALREADY_COMMITTED`, `ABORTED` | Inspect state | Follow returned state | Depends on state |
| `AMBIGUOUS_OUTCOME`, `RECONCILIATION_REQUIRED` | Inspection/reconciliation only | Reconcile or obtain operator resolution | Yes/unknown |
| `RECONCILIATION_UNAVAILABLE` | Yes for the same read-only probe | Retry probe later; remain blocked | Yes/unknown |
| `OPERATOR_UNAUTHORIZED` | No without authority | Obtain proper authorization | Yes/unknown |
| `STORAGE_UNAVAILABLE`, `EVIDENCE_RECORDING_FAILURE` | Inspect before mutation retry | Treat command outcome as unknown | Yes/unknown |
| `INTERNAL_PROTOCOL_ERROR` | Only after inspection | Escalate; fail closed | Unknown |

# 5. Illustrative JSON

This is illustrative, not the final schema:

```json
{
  "protocol_version": "v1alpha1",
  "message_type": "claim_effect",
  "message_id": "msg-01J...",
  "created_at": "2026-09-03T10:20:30Z",
  "agent_id": "agent.example",
  "run_id": "run-42",
  "dispatch_id": "dispatch-7",
  "request_id": "business-operation-884",
  "tool_id": "external_operation",
  "execution_scope": {"tenant": "tenant-a", "entity": "record-9"},
  "canonical_input": {
    "operation": "update",
    "entity": "record-9",
    "value": "new-value"
  },
  "canonicalization_version": "jcs-1",
  "effect_class": "keyed_mutate",
  "capability": "queryable",
  "policy_version": "policy-12",
  "provider_idempotency": {
    "key": "engine-derived-effect-key",
    "valid_for_seconds": 86400
  }
}
```

The engine reply adds `effect_id`, `claim`, `owner_id`, `lease`, and `fence`. A
client-provided `effect_id` is only a preview or consistency check and is never
trusted over the engine's derivation.

The companion `transition-envelope.schema.json` gives a conservative structural
schema for envelope projections. It intentionally does not attempt to encode all
cross-field CAS rules. Those belong to the engine and conformance suite.

## 5.1 Required examples

### First execution

1. Client may call `derive_effect_identity` to preview the engine-derived ID.
2. Client calls `claim_effect` with the complete identity and validated decision.
3. Engine returns `EXECUTE`, owner, lease, fence, and an `ATTEMPTING` projection.
4. Client reports `maybe_crossed` immediately before calling the provider.
5. Client may attach a provider reference, then reports `complete` or `fail`.
6. A successful completion returns a `COMMITTED` projection and stored result.

### Duplicate dispatch with stored result

A second dispatch submits the same business identity and canonical input. The
engine resolves the existing `effect_id`, returns `COMMITTED` and the stored result,
and does not return provider execution authorization.

### Active-owner polling

A second worker receives `WAIT_FOR_OWNER` and no execution authorization. It
inspects the effect again later. If the owner completes, the next claim returns
`RETURN_STORED_RESULT`. If the lease expires, takeover is considered only under
the engine's worker-death and effect-state policy.

### Argument drift

A second message reuses the business request identity but changes an identity-
relevant field. The engine returns `ARGUMENT_DRIFT`; it does not silently treat the
call as the old effect or execute it under the old claim.

### Stale fence rejection

Worker A holds fence 4. Worker B validly takes over with fence 5. A sends
`complete_effect` with fence 4. The engine returns `STALE_FENCE`; the stored row is
unchanged and no result is overwritten.

### Ambiguous provider outcome

The client reports `maybe_crossed` and a timeout. The engine moves to `UNKNOWN`.
A duplicate claim receives the `UNKNOWN` disposition, never automatic execution.

### Reconciliation outcomes

* `COMPLETED`: engine stores the reconciler's verified result and returns
  `COMMITTED`; no provider call occurs.
* `NOT_EXECUTED`: engine performs a fenced CAS to an eligible retry state. A later
  claim may receive execution authorization once.
* `UNKNOWN`: engine preserves `UNKNOWN` and requires later reconciliation or an
  operator.

### Policy denial

The engine records a denied decision and transitions `INTENDED` to `ABORTED`
before returning `POLICY_DENIED`. No provider-boundary event or provider call is
authorized.

### Operator resolution

An authenticated operator submits verified evidence that the provider effect was
completed. The engine records one-shot operator evidence, transitions to
`COMMITTED`, and future dispatches return the stored/operator-confirmed result.

# 6. Serialization and canonicalization

## 6.1 Format recommendation

Use **JSON Schema 2020-12 plus OpenAPI 3.1** for the first contract:

* JSON Schema is easy to consume from TypeScript, Go, Rust, Java, and Python.
* JSON fixtures are reviewable in Git and suitable for conformance vectors.
* OpenAPI can describe HTTP transport after the data contract stabilizes.
* The engine needs cross-field and stateful validation that JSON Schema cannot
  express; those rules remain normative prose plus executable conformance tests.
* Protocol Buffers are a reasonable later transport for high-volume internal
  deployments, but would make the first public contract less readable and would
  complicate unknown-enum and flexible extension handling.

This is a proposal. A future performance-sensitive service may offer protobuf or
CBOR while preserving the same semantic schema and canonicalization rules.

## 6.2 Canonical identity

### Current Python behavior

The current implementation uses SHA-256 over compact JSON produced by
`transition.canonical_json()`, which sorts object keys and uses `default=str`.
`args_fingerprint()` hashes `args` and bookkeeping-filtered `kwargs`. The transition
preimage then includes the schema marker, scope (`thread_id`, `run_id`, `node`),
tool, argument fingerprint, destination fingerprint, side-effect class,
`agent_id`, `policy_version`, and optional `dispatch_id`. A configured provider
idempotency-key parameter is excluded from the transition argument fingerprint so
a retry with a changed provider key reaches the same transition and can be rejected
explicitly. This behavior is covered by identity and provider-key tests, but it is
not a portable serialization specification.

### Proposed wire behavior and deliberate changes

Define `canonicalization_version = jcs-1` initially, using a published canonical
JSON profile equivalent to RFC 8785 JCS where applicable, with these Mycelium
rules. This is a deliberate wire-level change and must not be silently substituted
into the existing Python helper before a compatibility plan exists:

* Object keys are sorted lexicographically by their UTF-16 code-unit ordering as
  required by JCS. No language's native map iteration order is used.
* Arrays preserve order. Arrays that are logically sets must be explicitly sorted
  by the tool contract before identity is formed.
* Strings are valid Unicode scalar strings. Do not silently normalize Unicode;
  NFC normalization is a contract choice and must be applied consistently if
  adopted. The initial recommendation is to preserve code points and require the
  tool contract to normalize semantic identifiers explicitly.
* Integers are represented without leading zeros and without loss of precision.
  Identity-bearing integers must be in the signed IEEE-754 safe-integer range
  `[-9007199254740991, 9007199254740991]` so JavaScript and other clients agree.
* Raw floating-point values are rejected from identity-bearing input. JCS defines
  deterministic number serialization, but accepting language floats still creates
  semantic ambiguity around precision and signed zero. Financial or exact decimal
  values must be encoded as strings with a declared decimal format, or as integer
  minor units.
* Timestamps use RFC 3339 UTC with a required `Z`, fixed precision policy, and no
  alternate timezone spelling in identity-bearing fields.
* Missing and `null` are different. An absent optional field is omitted; explicit
  null is included. A tool contract may define an equivalence, but it must be
  versioned.
* Binary data is encoded as base64url without padding with an explicit media/type
  marker. Raw language byte arrays are not accepted.
* Enums use their registered wire strings, not language enum ordinals.
* URLs use the `url-1` tagged profile below. Scheme and DNS host are lowercase;
  path, query ordering, repeated parameters, percent-encoding, explicit ports, and
  trailing slashes are preserved. Credentials, fragments, relative URLs, control
  characters, and schemes outside `http` and `https` are rejected.
* Generic exact decimals use the `decimal-1` tagged profile below. Raw JSON numbers
  are never decimal quantities in identity-bearing data.
* Maps with non-string keys are rejected. Encode them as arrays of key/value
  objects with an explicit ordering rule.
* Unsupported values such as functions, class instances, cyclic objects, dates
  without a declared timestamp field, and undefined values are rejected rather
  than stringified.
* Secret references are structured opaque references such as
  `{ "secret_ref": "provider/key" }`. Secret material must never enter
  canonical input, identity preimages, errors, outcomes, or protocol logs.
* Fields excluded from identity are defined by the registered tool contract and
  canonicalization version. Clients cannot exclude fields ad hoc.

### Typed identity profiles

`decimal-1` is the generic exact-decimal representation:

```json
{"$type":"decimal","profile":"decimal-1","value":"1500.25"}
```

The value is ASCII and already normalized. It has an optional leading minus, no
plus sign, exponent, whitespace, commas, or negative zero; digits have no leading
zeros except `0`, fractional trailing zeros are forbidden, and an integral value
has no decimal point. Zero is exactly `"0"`. The limit is 38 total digits and a
maximum scale of 18. Other forms, including `"1.0"`, are rejected rather than
silently normalized. Money should normally use integer minor units and an explicit
currency when that currency defines a safe minor unit. This does not make minor
units suitable for every financial quantity. Provider-specific numeric formats
remain tool-contract values and must not be confused with this generic type.

`url-1` is a tagged validated URL, not a provider-resource equivalence engine:

```json
{"$type":"url","profile":"url-1","value":"https://example.com/resource"}
```

The value must be an absolute UTF-8 URL with an `http` or `https` scheme, a
non-empty DNS or IP host, no userinfo, fragment, or controls. The scheme and DNS
host are lowercased for identity; the path, query bytes and order, repeated query
parameters, percent-encoding, explicit port, and trailing slash are preserved.
Non-ASCII DNS names must be supplied in their ASCII IDNA form. No dot-segment,
slash, default-port, query sorting, or provider-specific normalization is applied.
The normalized tagged object, including its type marker, participates in JCS.
Applications needing another scheme or URL equivalence must define a new typed
profile and version it. Ordinary strings remain appropriate when a provider treats
URL text as opaque data.

### Finalized proposed identity contract

The proposed `identity-v1` preimage is exactly this object, with no additional
members permitted:

```json
{
  "application_id": "app.example",
  "business_request_id": "request-884",
  "canonicalization_version": "jcs-1",
  "destination": {"id": "record-9", "kind": "record"},
  "execution_scope": {"entity": "record-9", "tenant": "tenant-a"},
  "identity_version": "1",
  "input": {"operation": "update", "value": "new-value"},
  "tenant_id": "tenant-a",
  "tool_contract_version": "1",
  "tool_id": "external_operation"
}
```

The exact member set is frozen for this draft. The engine creates and validates
all members, while the host supplies candidate values for business identity,
scope, destination, and input. The engine derives `effect_id` only after typed
normalization, contract validation, tenant binding, and canonicalization.

Included fields:

* `identity_version` and `canonicalization_version` select the identity rules.
* `tenant_id` and the tenant component of `execution_scope` prevent cross-tenant
  collisions. They must come from authenticated deployment context, not only JSON.
* `application_id` prevents independently authorized applications from
  collapsing effects. `agent_id` is **excluded** from the finalized identity-v1
  preimage. It is authenticated provenance and policy input, not logical effect
  identity. Two workers or logical agents authorized by one application to carry
  out the same business request must converge on one effect. If deployments need
  separate effect domains, they must use distinct `application_id` or tenant
  namespaces. This is a deliberate change from the current Python preimage and
  removes a retry-fragmentation risk.
* `business_request_id` is a host-owned stable operation key and is part of the
  effect identity. It is not the transport message ID and not merely a lookup
  alias. Reusing it with changed meaningful input is an `ARGUMENT_DRIFT` conflict.
* `tool_contract_version` separates incompatible meanings of one `tool_id`.
  A contract-version change always creates a new identity namespace. The engine
  must not use the new contract to mutate or reinterpret an old record. The host
  must migrate intentionally, usually by creating a new business request identity
  and linking it to the old operation for audit. `effect_class`, recovery
  capability, and policy version are contract/decision metadata and are excluded
  individually.
* `tool_id`, `input`, `destination`, and identity-relevant execution scope identify
  what the provider effect means. Destination is omitted only when the registered
  tool contract says it has no destination.

Explicitly excluded: `agent_id`, protocol/schema version, dispatch/run/trace/span IDs, owner,
lease, fence, timestamps, provider key/reference/response, reconciliation verdict,
operator identity, outcome metadata, and policy-decision evidence. These describe
delivery, execution, recovery, or audit rather than the logical effect. A policy
version change is handled by revalidation against the stored contract and decision,
not by silently creating a second effect.

The proposed hash construction is:

```text
canonical_bytes = UTF8(JCS(identity_preimage))
hash_input = UTF8("mycelium.effect.v1\\n") || canonical_bytes
digest = SHA-256(hash_input)
effect_id = "mycelium:effect:v1:" || lowercase_hex(digest)
```

This construction is **approved for identity-v1** and is covered by the static
fixture vectors. SHA-256 is sufficient for collision resistance in this
identifier role. A keyed hash is not required because identity is not intended to
be secret; access control and tenant authentication provide isolation. The maximum
canonical preimage size is 64 KiB. Larger input must fail with
`IDENTITY_PREIMAGE_TOO_LARGE`. The hash is not confidentiality: low-entropy input
can be guessed from its digest.

The final member set, domain separator, size limit, digest encoding, and prefix
must not be changed without an identity-version or protocol compatibility
procedure. The fixtures in `fixtures/effect-identity.json` are the first static
vectors for this construction.

RFC 8785 JCS handles JSON object ordering and number serialization, but it does
not define application semantics for decimals, timestamps, URLs, binary values,
secret references, or set-like arrays. Mycelium restrictions above are therefore
part of `jcs-1`. NaN, positive/negative infinity, and unsupported language
objects must fail validation, never pass through `default=str` or an equivalent
fallback.

## 6.3 Business request identity cases

| Case | Proposed result | Reason |
|---|---|---|
| Same business request, tool, destination, and meaningful input | Same `effect_id`; duplicate dispatch returns the stored state/result. | All identity members match. |
| Same business request with changed meaningful input | `ARGUMENT_DRIFT`; no silent second execution under the old request alias. | The candidate hash differs, but the stable business alias exposes an identity conflict. |
| Same business request with a different tool | Different effect identity, or reject if the host contract reserves the request key to one tool. | `tool_id` is identity-bearing. The engine must not merge tools. |
| Same business request with a different destination | `ARGUMENT_DRIFT` or explicit destination conflict. | Destination is both identity and authorization relevant. |
| Different business request with identical tool/input | Different `effect_id`. | Two host operations may intentionally produce two effects. |
| Same operation with a new dispatch ID | Same wire `effect_id`. | Dispatch is excluded from `identity-v1`; this differs from current Python. |
| Same operation with a new run ID | Same wire `effect_id`. | Run is correlation/context metadata, not effect identity. |
| Policy version changes during retry | Same `effect_id`; engine revalidates policy. A terminal denial remains terminal; reauthorization requires an explicit, audited migration/operator action, normally with a new business request identity. | Policy version is excluded to avoid identity fragmentation, but old decisions are never rewritten or automatically retried. |
| Host accidentally reuses a business request ID | Reject on meaningful identity mismatch; do not guess which operation was intended. | Stable request identity is a host responsibility. |
| Host generates a random business request ID on every retry | Different effects are possible and the engine cannot infer equivalence. | The host has violated the identity contract; reconciliation or business-level repair is required. |

`business_request_id` is therefore both an identity member and a stable host-owned
business key. It is not a second independent deduplication algorithm. The engine
may maintain a request-to-effect alias index for diagnostics and drift detection,
but `effect_id` remains the canonical effect record key.

# 7. Sidecar and deployment architecture

## 7.1 Provider-call placement

The development sidecar owns the protocol and durable ledger, but the provider call
should remain in the application process. The application performs this sequence:

1. claim and receive owner/fence;
2. obtain the engine's policy decision;
3. report boundary entry immediately before provider invocation;
4. call the provider using the engine-approved provider key where applicable;
5. report provider reference and completion or failure.

This preserves the existing provider-boundary truth model. There is an unavoidable
crash window between a provider call and reporting completion. Reconciliation or
operator resolution handles that window; the sidecar cannot infer it from a lost
HTTP connection.

An engine-owned provider adapter can later move the call into a trusted execution
worker, but that is a different trust and secret-handling model, not a transparent
sidecar optimization.

## 7.2 Deployment comparison

| Model | Trust/authentication | Failure and latency | Storage/lifecycle | Operations and suitability |
|---|---|---|---|---|
| Local subprocess | Loopback HTTP bearer token; Unix peer credentials are optional hardening. | Low latency; sidecar crash pauses claims but durable state survives if storage is external/durable. | Process supervisor owns restart; engine owns storage access. | Development-only prototype profile. |
| Unix domain socket | File permissions plus peer credentials and nonce/session auth. | Lower exposure than TCP; reconnect handling required. | Socket lifecycle follows engine process. | Strong local default; not cross-host. |
| Localhost HTTP | mTLS or signed local token still needed; localhost is not a trust boundary by itself. | Easy tooling; serialization/network overhead; port hijacking risk if unauthenticated. | Process/service manager owns lifecycle. | Useful compatibility transport, not sufficient auth by default. |
| Container sidecar | Service-account identity, network policy, secret injection, authenticated channel. | Container restarts and network partitions must return fail-closed errors. | Shared durable Redis/Postgres or sidecar-owned volume with topology limits. | Good deployment unit; requires explicit storage durability. |
| Shared internal service | mTLS, tenant identity, authorization, quotas, request replay protection. | Network failures and queueing; horizontally scalable engine. | Central durable storage and migrations owned by service. | Best for many applications; larger operational trust boundary. |
| Remote multi-tenant service | Strong tenant auth, mTLS, scoped tokens, isolation, encryption, audit. | Highest latency and availability complexity; fail closed on uncertain command outcome. | Service owns storage, retention, backup, migrations, and tenant namespaces. | Later scope; requires service-level product and security work. |

### Development-only prototype

The reference adapter is `sdk/mycelium/sidecar.py` and is intentionally one
transport module, not a second ledger. It uses `FileLedgerStorage` and the existing
`ActionLedger` claim, decision, lease, boundary, completion, failure, and effect
lookup methods. The adapter derives identity-v1 before calling the engine and uses
an internal effect-ID handoff so the engine stores the same derived identity without
changing legacy wrapper derivation.

Configure it with an absolute YAML file containing `kind: mycelium-sidecar`, a
loopback literal host, one tenant and application, an owner-only bearer-token file containing exactly 43 base64url characters or 64
hexadecimal characters,
absolute file-ledger and outcome paths, `identity-v1`, and a request-body limit.
Start it with `mycelium sidecar serve --config /absolute/path/sidecar.yaml`.
The token is sent only as `Authorization: Bearer ...`; it is never a command-line
value. `/health` is the only unauthenticated endpoint. Authenticated endpoints are
`/v1/capabilities`, `/v1/openapi.json`, `/v1/identities/derive`,
`/v1/effects/claim`, `/v1/effects/{effect_id}`, and the fenced action endpoints
`renew`, `boundary`, `provider-reference`, `reconcile`, `complete`, and `fail`.
A claim must carry the existing engine's validated decision evidence before it
returns `EXECUTE`; the HTTP layer does not evaluate policy. Reconciliation invokes
the engine's configured reconciler and fails closed when unavailable.

This prototype has no provider adapter, automatic legacy migration, remote binding,
operator reauthorization endpoint, hostile-client protection, or exactly-once
claim. It is suitable for a non-Python client using ordinary HTTP and JSON.

Minimal configuration shape:

```yaml
kind: mycelium-sidecar
protocol_version: "v1alpha1"
identity_namespace: identity-v1
tenant_id: tenant-a
application_id: app-a
bearer_token_file: /absolute/path/sidecar.token
ledger: {type: file, path: /absolute/path/sidecar-ledger.json}
outcome_storage: {type: file, path: /absolute/path/sidecar-outcomes.ndjson}
server: {host: 127.0.0.1, port: 8787}
```

A language-neutral client sends `Authorization: Bearer TOKEN` and JSON, for
example `curl -H 'Authorization: Bearer TOKEN' http://127.0.0.1:8787/v1/capabilities`.
It must retain the returned owner and fence, report `boundary` immediately before
the provider call, then report `complete` or `fail` using the same fence.

### Transport decision

The first implementation should use **localhost HTTP with JSON and an OpenAPI
3.1 description**, with the engine also available as a supervised local
subprocess. This is a deliberate refinement of the earlier Unix-socket-first
proposal. HTTP wins for the first cross-language experiment because TypeScript,
Go, Java, Rust, and local debugging tools have mature clients; JSON examples are
inspectable; polling and health endpoints are straightforward; and the same
contract can move from a local process to a container without changing message
semantics. A Unix socket remains a useful hardening option for single-host
production, but it makes non-Python client setup and container networking less
obvious. Standard-input/output is convenient for a test harness but poor for
multiplexing and long-lived polling. gRPC is a credible later high-throughput
transport after the JSON semantics stabilize, but premature protobuf-first design
would obscure the reviewable contract. Remote HTTP is a deployment profile, not a
separate semantic protocol.

Local clients must authenticate even on localhost, using a sidecar-issued local
session token, peer credentials where available, or mTLS in container/remote
profiles. A bearer token must be scoped to application/tenant and capability; it
must not be treated as proof that the caller is an operator or reconciler.
Transport timeouts mean only that the reply was not observed. They never imply
provider failure or permission to retry execution. Commands with a `message_id`
may be safely retried at the transport layer only when the operation's protocol
semantics say so; the engine must return the authoritative current record.

Deferred transports: Unix-socket-only hardening, gRPC/protobuf, remote multi-tenant
service, and provider execution inside the sidecar.

## 7.3 Sidecar limitations

A sidecar cannot prevent an application from directly calling a provider. It can
only protect calls routed through it or through a client that truthfully reports
boundary events. Doctor can detect configured coverage in some Python deployments,
but it cannot prove that arbitrary code never bypasses the sidecar. Verify proves
named scenarios, not every business call site.

A compromised application client can forge provider-boundary events unless the
provider call is moved into a trusted engine-owned adapter or the provider itself
attests the operation. The protocol must therefore state whether a deployment's
client is trusted, merely fallible, or adversarial.

# 8. Thin TypeScript client

The TypeScript package is a **transport client**, not a second runtime. Its
responsibilities are:

* validate basic JSON shapes and supported protocol versions;
* encode typed decimal and URL values using the published profiles;
* send commands over authenticated loopback HTTP and parse replies;
* propagate host-owned business, tenant, application, tool, and scope identity;
* retain the returned owner/fence handle and report provider-boundary events;
* return stored results without invoking the provider when the engine says so;
* map error codes into ergonomic error objects;
* redact secrets from logs and error objects;
* provide a helper for one separately represented external effect.

It must not implement state transitions, authoritative hashes, claim arbitration,
reconciliation authorization, or operator rules.

Shipped `0.1.0` API:

```ts
import { MyceliumClient } from "@mycelium-labs/sidecar-client";

const client = new MyceliumClient({
  baseUrl: "http://127.0.0.1:8787",
  token: process.env.MYCELIUM_SIDECAR_TOKEN!,
  tenantId: "tenant-a",
  applicationId: "app-a",
});

await client.assertCompatible();
const claim = await client.claimEffect({
  businessRequestId: "business-operation-884",
  toolId: "external_operation",
  toolContractVersion: "1",
  destination: { entity: "record-9" },
  executionScope: { tenant: "tenant-a" },
  input: { operation: "update", value: "new-value" },
  decision: { allowed: true, verdicts: [], denied_reasons: [] },
});

if (claim.disposition === "RETURN_STORED_RESULT") return claim.result;
if (claim.disposition === "EXECUTE") {
  await client.recordBoundary(claim.handle, { boundary: "maybe_crossed" });
  const result = await provider.call();
  await client.completeEffect(claim.handle, { result });
  return result;
}
// Every other disposition means: do not call the provider.
```

The wrapper can make the common one-effect case ergonomic, but it must require the
application to expose a boundary callback or provider adapter. A function that
performs three external effects cannot safely resume at its second internal point
from one envelope. Those effects must be separate envelopes, or the application
must provide a custom reconciler and recovery plan.

# 9. Versioning and compatibility

`v1alpha1` is the first frozen development protocol revision. Implementations
must advertise and require that exact value. The identity and canonicalization
contracts remain independently versioned as `identity-v1` and `jcs-1`.

The contents of `v1alpha1` are immutable. A wire-visible breaking change,
including changing a required field, route, disposition meaning, error meaning,
or canonical representation, requires a new protocol revision such as
`v1alpha2`. A correction that does not alter observable wire behavior may retain
the version. Additive fields are permitted only through the extension rules
already defined by this revision; they cannot silently become identity-bearing
or execution-authorizing.

* Negotiate a protocol version and feature capabilities before commands.
* Additive fields are optional and ignored when unknown unless marked critical by
  an extension namespace.
* Unknown enum members are preserved as opaque values in storage but treated as
  unsupported for execution. A client must never default an unknown state to a
  safe-to-retry state.
* Breaking changes require a new major protocol version and an explicit migration
  path. Existing records remain readable under their recorded version.
* `minimum_client_version` and server capabilities are discovery metadata, not
  substitutes for semantic validation.
* Storage migrations are engine-owned. Rolling upgrades must support reading old
  records and writing a version understood by all active workers.
* Policy-version changes do not silently rewrite past decisions. A new dispatch
  may be denied or require a new policy decision according to policy.
* Canonicalization-version changes are especially sensitive: they can create a
  different `effect_id` for the same business operation or collapse two operations
  unexpectedly. The version belongs in the identity domain, and a transition must
  retain the version used to derive its ID.
* Clients may use a compatibility mode for old Python APIs, but the wire record
  must retain unambiguous protocol fields.

# 10. Security and trust model

| Input | Trust level | Engine treatment |
|---|---|---|
| Tool contract, effect class, policy version | Configuration/engine trusted | Resolve from registered configuration; reject client attempts to loosen it. |
| Business request identity and scope | Host-supplied, potentially untrusted | Authenticate host, validate shape, bind tenant, and derive identity only after checks. |
| Canonical input | Client candidate | Canonicalize/validate independently; reject unsupported or drifted values. |
| `effect_id` | Engine-derived | Never accept a client hash as authoritative. |
| Owner and fence | Engine/storage authoritative | Authenticate caller and require exact current fence for mutations. |
| Lease time | Engine clock authoritative | Client heartbeats cannot extend beyond policy or resurrect a stale owner. |
| Provider reference/boundary | Application/provider observation | Store as evidence; require owner/fence; do not infer completion from transport success. |
| Reconciliation verdict | Registered read-only reconciler | Authenticate, scope, validate evidence, and fail closed on errors or unknown. |
| Operator resolution | Authenticated operator/authorizer | Require one-shot authorization, reason, and current state. |
| Result/failure | Application candidate | Sanitize, validate ownership, and store only through fenced mutation. |
| Outcome evidence | Engine/emitter | Durable append/export requirements are deployment-specific and observable. |

Threat responses:

* Forged request identity is mitigated only if the host supplies identity from a
  trusted business record. Mycelium cannot know whether the host chose the right
  order/entity ID.
* Forged provider references are not completion proof. A read-only reconciler or
  provider attestation must verify them.
* Stale workers are stopped by owner and monotonic fence checks on every mutation.
* Replayed messages are harmless when message dedupe and effect dedupe are kept
  distinct and state-changing commands are CAS-protected.
* Fence manipulation fails because fences are engine/storage-generated.
* Malicious reconciliation is a credential and service trust problem. Register
  only read-only, tenant-scoped reconcilers and audit every verdict.
* Argument drift is rejected when identity-relevant canonical input changes.
* Cross-tenant collisions are prevented by tenant/scope in the identity preimage
  and storage namespace. This requires a truthful authenticated tenant binding.
* Secrets are excluded from canonical input, evidence, outcomes, and logs. Secret
  references may identify a lookup, not reveal material.
* Unauthorized operator resolution requires engine-side authentication and policy.
* Sidecar bypass remains possible when the application can call the provider
  independently. This is an explicit unsupported boundary unless provider access
  is isolated behind the engine.
* Transport interception requires authenticated encryption and replay protection.
* Lost outcome evidence is a monitoring/durability failure. It does not change the
  effect state and must be surfaced rather than silently treated as success.

# 11. Guarantee map

| Desired guarantee | Protocol mechanism | Authoritative component | Required assumption | Existing Python evidence | Protocol conformance | Unsupported boundary | Maturity |
|---|---|---|---|---|---|---|---|
| Duplicate suppression | Canonical `effect_id`, durable lookup, stored-result reply | Engine + storage | Same logical input and durable namespace | Effect-id index tests; `ActionLedger` claim paths | Duplicate claim/dispatch vectors | Direct provider bypass | Current Python; frozen `v1alpha1` wire |
| Stable effect identity | Versioned canonical preimage | Engine | Host identity is semantically complete | `derive_effect_id_for_call`, identity tests | Cross-language canonical fixtures | Bad host identity | Implemented `identity-v1` prototype |
| Argument-drift rejection | Canonical input comparison and identity policy | Engine | Contract identifies meaningful fields | `ledger_identity.py`, args-drift tests | Same request with changed input | Unregistered arguments | Current Python; frozen `v1alpha1` wire |
| Single active ownership | Atomic claim and lease | Engine + storage | CAS-capable durable backend | storage backends, contention tests | Concurrent claim scenarios | Non-durable memory across workers | Current Python; frozen `v1alpha1` wire |
| Stale-worker fencing | Monotonic fence on every write | Engine + storage | All mutations route through engine | atomicity/fence tests | Takeover then stale writes | Unprotected direct provider call | Current Python; frozen `v1alpha1` wire |
| Durable completion | Fenced completion with stored result | Engine + storage | Result write succeeds | completion and outcome tests | Crash/retry completion vectors | Provider result not reported | Current Python; frozen `v1alpha1` wire |
| Conservative ambiguity | Boundary state and `UNKNOWN` fail-closed | Engine | Client truthfully reports boundary | TLA+, reconcile, UNKNOWN tests | Ambiguous timeout vectors | Compromised client can lie | Current Python; frozen `v1alpha1` wire |
| Reconciliation | Read-only verdict and CAS resolution | Engine + reconciler | Provider query is truthful/read-only | `reconcile.py`, reconcile tests | completed/not-executed/unknown vectors | Provider cannot be queried | Current Python; frozen `v1alpha1` wire |
| Provider key reuse | Engine-selected stable key and validity check | Engine + provider | Provider honors key semantics | provider-key tests | same-key retry/expiry vectors | Provider key TTL undocumented | Current Python; frozen `v1alpha1` wire |
| Policy denial before execution | Atomic decision before `ATTEMPTING` | Engine policy | Policy facts are truthful and available | `decision.py`, decision tests | denial-before-boundary vectors | Host bypass or bad adapter | Current Python; frozen `v1alpha1` wire |
| Operator resolution | Authenticated one-shot resolution | Engine + authorizer | Operator evidence and backend access trusted | operator release tests | authorization/replay vectors | Compromised operator credentials | Current Python; frozen `v1alpha1` wire |
| Durable outcome evidence | Append/emitter/export records | Engine/storage/exporter | Durable configured sink and monitoring | outcome/audit tests | emission failure and retrieval vectors | Dashboards/paging are host-owned | Current Python; frozen `v1alpha1` wire |
| Multi-worker behavior | Shared durable CAS and fences | Engine/storage | Correct topology and persistence | Redis proof and atomicity tests | process/concurrency harness | Memory backend | Current Python; frozen `v1alpha1` wire |
| Cross-language compatibility | Shared schema, canonical fixtures, reference engine | Protocol authority | Clients do not implement policy | Python sidecar with TypeScript and Go clients | Fixture and reference-server checks | Independent clients cannot be trusted as engines | Implemented development prototype |

## 11.1 Threat-model guarantee matrix

`Enforced` means the engine can reject the violating operation. `Enforced under
documented assumptions` includes durable storage and authenticated, truthful host
inputs. `Detectable` means evidence can expose the problem but cannot prevent it.

| Guarantee | Correct client | Fallible client | Hostile client |
|---|---|---|---|
| Stable identity | Enforced | Enforced under authenticated host identity | Enforced only for requests reaching the engine; bypass unsupported |
| Duplicate suppression through sidecar | Enforced | Enforced | Enforced only for sidecar-routed calls |
| Argument-drift detection | Enforced | Enforced | Enforced for submitted requests; forged semantics remain possible |
| Stale-fence rejection | Enforced | Enforced | Enforced at the engine boundary |
| Conservative ambiguous recovery | Enforced | Enforced under truthful boundary reports | Detectable only; client can lie |
| Tenant isolation | Enforced under token binding | Enforced under token binding | Requires deployment isolation |
| Provider completion truth | Enforced only with trusted provider evidence | Detectable, not guaranteed | Unsupported without attestation or trusted execution |
| Direct-call prevention | Enforced only by provider/network isolation | Unsupported | Unsupported |
| Policy enforcement | Enforced under authenticated policy inputs | Enforced under authenticated policy inputs | Requires trusted policy and authorizer boundary |
| Outcome evidence integrity | Enforced under durable engine storage | Enforced under durable engine storage | Detectable only if client can forge submitted evidence |

The prototype claims no exactly-once execution and no protection against a process
that holds provider credentials and bypasses the sidecar.

# 12. Conformance strategy

The repository ships a baseline language-neutral suite with a versioned
identity fixture and the Python sidecar as its reference engine. The local
runner executes raw HTTP, TypeScript, and Go against one temporary sidecar and
checks compatibility, identity, authentication, completion replay, stale
fencing, unknown-value rejection, and timeout uncertainty.

The broader conformance strategy builds on that baseline:

* **Serialization conformance:** every client accepts valid examples, rejects
  malformed required fields, preserves unknown extension fields, and never emits
  secrets.
* **Canonicalization conformance:** each language produces identical canonical
  bytes and identity preimage for fixture inputs, including negative cases.
* **Client conformance:** client propagates IDs, retains fence/lease, reports
  events, handles duplicate replies, and never locally authorizes a retry.
* **Engine conformance:** the reference engine passes all state transition, CAS,
  ownership, reconciliation, and policy fixtures.
* **Storage conformance:** a backend proves atomic claim, fenced mutation,
  durability, namespace isolation, and migration behavior.
* **Provider-adapter conformance:** an adapter is read-only, scoped, conservative,
  and returns the three reconciliation verdicts correctly.
* **Deployment verification:** Doctor-like checks verify configured transport auth,
  storage topology, sidecar coverage evidence, version compatibility, and outcome
  durability. It cannot prove arbitrary bypass absence.

Static fixtures cover serialization, canonicalization, state matrices, and
error codes. The local runner now covers one approved identity vector and core
client lifecycle against the reference server. Expanding it across the full
negative fixture corpus, concurrency, lease takeover, controlled process
crashes, provider-boundary ambiguity, and recovery remains future work. Tests
must distinguish a compliant client from a compliant engine; a client passing
fixtures does not establish that it is safe to run an independent state
machine.

# 13. Roadmap and implementation status

| Phase | Status | Deliverable | Remaining risk or exit criterion |
|---|---|---|---|
| 0 | Complete | Terminology and evidence audit | Keep claims linked to implementation evidence. |
| 1 | Complete | Protocol RFC and approved decision log | Breaking changes require a new protocol revision. |
| 2 | Complete | JSON Schema, OpenAPI, canonical fixtures, and error registry | Preserve fixture compatibility under `v1alpha1`. |
| 3 | Development implementation complete | Local reference sidecar over authenticated loopback HTTP | Not a production deployment profile. |
| 4 | Development implementation complete | Thin TypeScript transport client | Remains experimental and contains no local transition authority. |
| 5 | Local development complete | Cross-language fixtures, a thin Go client, and a repeatable local conformance kit in CI | Expand the executable corpus for concurrency, process crashes, and every negative fixture. |
| 6 | Later scope | Framework integrations and container sidecar | Requires bypass, authentication, secrets, observability, and upgrade design. |
| 7 | Later scope | Independent engine experiments, only if justified | Requires formal compatibility proof and an operational reason to duplicate the engine. |

## Implemented development release

The development release contains the versioned schema, canonicalization
fixtures, loopback HTTP adapter around the Python engine, and thin TypeScript
and Go clients. It does not include a remote multi-tenant service, provider
calls inside the sidecar, or an independent non-Python state machine.

# 14. Decisions and approved prototype boundary

1. **Who derives `effect_id`?** The engine. Clients may calculate previews only.
2. **Where does the provider call execute?** In the host application first. Move it
   into an engine-owned adapter only with a new trust/secret model.
3. **First transport?** Loopback-only HTTP with a high-entropy bearer token and
   JSON messages. Unix-socket peer credentials are optional hardening; remote HTTP
   is prohibited by the prototype profile.
4. **TypeScript package?** An experimental transport client over the frozen raw
   API, with no independent ledger or transition authority.
5. **How report the provider boundary?** Explicit client events immediately before
   and after the call, plus optional provider references. The crash window remains
   and requires reconciliation; no callback can remove it completely.
6. **What stays server-side?** Effect identity verification, policy evaluation,
   claim arbitration, leases, fences, state transitions, reconciliation acceptance,
   operator resolution, and authoritative result selection.
7. **Doctor and Verify?** They should remain evidence interfaces. Later they can
   inspect a sidecar through a separate diagnostics protocol, but ordinary effect
   commands should not be overloaded with test/diagnostic semantics.
8. **What stops bypass?** Nothing in a client-only sidecar architecture. Enforce
   provider network access through a trusted execution worker or provider gateway
   if bypass prevention is a hard requirement.
9. **What if the client is malicious?** The current guarantees reduce to what the
   engine can observe and authenticate. A malicious client can lie about a provider
   call while it retains direct provider credentials. This is an unsupported
   hostile-client deployment boundary, not a JSON protocol guarantee.
10. **Is a sidecar enough?** It is enough for a first interoperability boundary,
    not for hostile-client guarantees. An SDK callback protocol is still required
    for host-executed provider calls.

## Explicit later scope

* Independent non-Python safety engines.
* Engine-owned execution workers for arbitrary providers.
* Cryptographic provider attestation of execution.
* Two-person operator approval and policy-engine integration.
* Cross-language native runtimes with identical state-machine semantics.
* Managed hosted dashboards, paging, retention, and multi-tenant service product.

# Appendix A. Evidence and terminology mapping

The current Python runtime has implementation-specific names that map to protocol
concepts as follows:

| Current concept | Protocol concept |
|---|---|
| `TransitionScope` | run/dispatch/execution scope context |
| `ToolTransitionBinding` | registered tool contract: class, capability, retry, spendability |
| `LedgerEntry` | stored record projection |
| `ActionLedger.claim_side_effecting` | `claim_effect` |
| `record_decision` | `record_decision` |
| `side_effect()` / `mark_maybe_crossed()` / `mark_crossed()` | `record_boundary` |
| `complete` / `fail` | `complete_effect` / `fail_effect` |
| `renew_lease` | `renew_lease` |
| `external_operation_ref` | provider operation reference |
| `Reconciler` / `ReconcileResult` | read-only provider adapter and verdict |
| `release` | authorized operator resolution |
| `Decision` / `PredicateVerdict` | policy decision evidence |
| `OutcomeEmitter` / audit receipt | outcome/evidence stream |
| Doctor | deployment/configuration evidence |
| Verify scenarios | controlled protocol/deployment evidence |

This appendix is informative. The wire contract must not require any of these
Python names.

## Approval matrix

| Area | Status | Evidence | Remaining limitation |
|---|---|---|---|
| Identity preimage and hashing | Approved | Frozen identity-v1 contract and effect fixtures | Requires truthful host identity |
| Canonicalization | Approved | RFC 8785 profile and Python/Node vectors | Typed semantics are profile-specific |
| Decimal and URL profiles | Approved | decimal-1, url-1 rules and fixtures | Other schemes/types require new profiles |
| Schema and secrets | Approved | JSON Schema, rejection codes, redaction rules | Aggregate size and secret scanning are engine duties |
| Policy changes | Approved | Immutable denial and explicit reconsideration operation | First prototype may return unsupported |
| Authentication and tenant binding | Approved | Loopback token principal, fixed tenant/application | Development-only, not human auth |
| Legacy compatibility | Approved | Separate namespace, read-only inspection, audited immutable alias | No automatic or active/unknown migration |
| Hostile-client model | Approved boundary | Correct/fallible guarantees matrix | Bypass and forged provider reports unsupported |
| Provider attestation | Approved optional | Extensible attestation evidence | Required only for stronger hostile deployments |
| Sidecar readiness | Frozen as v1alpha1 for development only | D17 scope and restrictions | Not production-ready |

## Recommendation

Use the frozen `v1alpha1` contract for development-only sidecar and client
experiments within the approved scope.
The architecture is viable if the engine remains authoritative and provider-boundary
truth is treated as an explicit host responsibility. The largest later-scope risk
is not serialization; it is the trust gap created when an application can bypass
the sidecar or report provider events dishonestly.

---

**Maturity labels used here:** current = implemented in the Python engine or
development sidecar; proposed = design beyond the implemented `v1alpha1`
profile; assumption = required external condition; unresolved = needs an
explicit design or deployment decision; later scope = intentionally excluded
from the first interoperability release.

# Appendix B. Audit evidence index

These are the principal implementation and test anchors reviewed for this draft.
They are evidence references, not protocol API requirements.

| Protocol claim | Implementation anchor | Existing test/proof anchor | Audit result |
|---|---|---|---|
| Effect identity includes scope, tool, argument fingerprint, destination, class, agent, policy, and optional dispatch in current Python | `sdk/mycelium/transition.py`: `build_transition_preimage()`, `derive_effect_id_for_call()` | `sdk/tests/test_effect_identity.py` | Current behavior is deterministic only within the Python serializer; portable wire identity is a deliberate change. |
| Explicit request identity is host-supplied and validated | `transition.py`: `parse_explicit_request_id()`, `request_id_from_argument()` | `test_explicit_request_id.py`, `test_request_identity_policy.py` | Client/host identity must be validated; random dispatch IDs are not business identity. |
| Bookkeeping fields are excluded from current argument fingerprint | `transition.py`: `_tool_kwargs()`, `args_fingerprint()` | `test_effect_identity.py`, `test_args_drift.py` | Exclusion list must become a versioned wire contract. |
| Claim is atomic and assigns owner/lease/fence | `ledger_storage.py`: `try_claim_inflight()`; durable backend overrides | `test_atomicity_contract.py`, `test_two_worker_envelope.py` | Preserved when storage is shared and CAS-capable. |
| Lease renewal is not provider progress | `ledger_context.py`: `_lease_auto_renew()`, `renew_lease()` | `test_lease_validity.py`, worker-death tests | Heartbeat proves liveness observation only. |
| Stale workers cannot mutate after takeover | `ledger_storage.py`: `try_transition()`; recovery/execution mutation calls | `test_atomicity_contract.py` stale-fence and resumed-worker cases | Fenced CAS is authoritative. |
| Decision is recorded before an allowed provider attempt | `decision.py`: `Decision`, `DecisionEngine`; `ledger_execution.py`: `_record_boundary_decision()` | `test_decision.py` | Policy facts must be sanitized and decision CAS must remain server-side. |
| Boundary is monotonic and distinct from effect state | `action_ledger.py`: boundary rank/advance; `ledger_context.py` | `test_side_effect_boundary.py` | Boundary is observation/evidence, not completion. |
| Completion is fenced and stores a result | `ActionLedger.complete()` and `ledger_recovery.py` | `test_atomicity_contract.py`, completion and failure tests | Provider return is not durable completion until the engine write succeeds. |
| Ambiguous outcomes do not auto-reexecute | `transition_resolution.py`, `ledger_recovery.py`, `ReconcileStatus` | `test_reconcile.py`, `test_tool_capability.py`, `effect_protocol_proof.py` | `UNKNOWN` remains blocked without verified resolution. |
| Provider operation references support read-only reconciliation | `ledger_context.py`: `record_external_operation()`; `reconcile.py` | `test_external_operation_ref.py`, `test_reconcile.py` | Reference is untrusted until reconciler verification. |
| Operator resolution is one-shot and authorized | `ledger_recovery.py`: `release()`; `operator_auth.py` | `test_operator_release.py` | Operator authority cannot be represented as an ordinary client field. |
| Outcome/audit evidence is separate from state mutation | `outcome_emit.py`, `audit_receipt.py`, `outcome_export.py` | `test_outcome_emit.py`, `test_outcome_export.py` | Evidence durability is a deployment assumption. |
| Doctor detects configured coverage/topology but does not execute tools | `doctor/engine.py`, `doctor/checks.py` | `test_doctor.py` | Diagnostic evidence cannot prove bypass absence. |
| Verify exercises named synthetic scenarios | `verify/engine.py`, `verify/scenarios/`, `verify/proof/` | `test_verify.py`, `test_effect_protocol_proof.py` | Verify is conformance/deployment evidence, not universal application certification. |

The review found no basis for claiming that a sidecar can atomically couple its
ledger write to an unrelated provider request, prove a host did not bypass it,
prove a heartbeat means provider progress, or resume an arbitrary multi-effect
function at an internal point. Those remain assumptions, unsupported boundaries,
or later scope as identified above.
