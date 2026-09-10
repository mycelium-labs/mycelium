# Language-neutral Transition Envelope

Mycelium's engine is written in Python. This protocol gives every other
language the same doorway into it.

A TypeScript, Go, Java, Rust, or other application sends ordinary HTTP/JSON to
a self-hosted Mycelium sidecar. The sidecar derives action identity, decides
whether execution may proceed, and records state transitions in the
authoritative Python engine. Language clients stay small: they format requests and parse
responses instead of duplicating the ledger, policy, fencing, or recovery state
machine.

```text
application in any language
          ↓ HTTP/JSON Transition Envelope
self-hosted Python sidecar
          ↓
authoritative Mycelium engine and ledger
```

Published experimental clients:

- TypeScript: [`@mycelium-labs/sidecar-client@0.1.1`](../../../clients/typescript/README.md)
- Go: [`github.com/mycelium-labs/mycelium/clients/go@v0.1.1`](../../../clients/go/README.md)
- Other languages: use the same authenticated OpenAPI contract directly.

This is language-neutral interoperability, not a separate Mycelium engine in
every language. `v1alpha1` provides a trusted-loopback development profile and
an explicitly selected shared PostgreSQL profile. Both are self-hosted and
experimental; neither is a public multi-tenant service or production IAM
boundary. Follow the [self-hosting guide](../SELF_HOSTING.md) to run either
profile.

## Language-neutral protocol design

- [v1alpha1 freeze and release checklist](V1ALPHA1_RELEASE_CHECKLIST.md): frozen
  identifiers, compatibility rules, validation targets, and explicit publication
  gates for the first experimental protocol revision.
- [Mycelium Transition Envelope](TRANSITION_ENVELOPE.md): frozen `v1alpha1` language-neutral
  envelope, state machine, operations, canonicalization, sidecar architecture,
  TypeScript client, security model, guarantee map, and roadmap.
- [Transition Envelope JSON Schema](transition-envelope.schema.json): JSON Schema
  2020-12 structural contract with separate command, reply, event, stored-record,
  and error definitions.
- [Identity fixtures](fixtures/README.md): approved canonicalization, decimal, URL,
  and effect-ID vectors for cross-language conformance.
- [Identity decisions](TRANSITION_ENVELOPE_DECISIONS.md): accepted draft decisions,
  compatibility notes, and remaining RFC questions.

`effect_state.tla` is a TLA+ model of the core `ActionLedger` state machine for
consequential tools. The Python proof harness in `mycelium.verify.proof` is the
executable conformance layer; this file is the compact formal sketch.

## OpenAPI contract

The sidecar serves the frozen `v1alpha1` machine-readable OpenAPI 3.1 contract at
`GET /v1/openapi.json`. It is generated directly from `mycelium.sidecar` so the
served document remains the single transport description. `/health` is the only
unauthenticated process probe, and `/ready` is the unauthenticated storage probe;
every `/v1` route uses the bearer scheme.

The served OpenAPI document is authoritative for implemented HTTP routes and
operation-specific payloads. The companion JSON Schema describes the broader
Transition Envelope projections, including stored records and reserved command
forms; its command names must not be interpreted as additional sidecar routes.

The implementation remains authoritative for runtime trust. Generated types do
not grant ownership, validate a fence, authorize a provider call, or resolve an
unknown outcome. Clients must fail closed on unknown safety-critical enum values.
The contract is intentionally experimental and language-neutral:

```text
TypeScript   Go   Java   Rust   Raw HTTP client
     \        |    |      |       /
              v
       Same Mycelium sidecar protocol
```

The `v1alpha1` OpenAPI is suitable for generator experiments and describes strict
request shapes, responses, claim dispositions, typed values, and protocol errors.
A standalone export command is intentionally deferred because the served document
is the only authoritative copy and does not require sidecar startup for inspection
of its Python source.

## Executable conformance

[`conformance/run.py`](../../../conformance/run.py) starts the real Python
sidecar on an operating-system-assigned loopback port and runs the same
synthetic lifecycle through raw HTTP, the TypeScript client, and the Go client.
It checks the approved identity fixture, authentication, completion replay,
stale fencing, unknown-value and malformed-reconciliation rejection, and
conservative timeout handling. Separate TypeScript and Go processes also race
for one effect, are killed before and after the provider boundary, and verify
fail-closed recovery plus durable replay after a sidecar restart.

```bash
sdk/.venv/bin/python conformance/run.py
sdk/.venv/bin/python conformance/run_postgres.py
```

The first command is the trusted-local compatibility check. The second starts
disposable PostgreSQL and two independent sidecars, then checks cross-sidecar
claim arbitration and client compatibility. The commands are also exercised in
CI, use synthetic effects, and never call an external provider. Passing them
does not prove internet-facing deployment safety, production IAM, or provider
truth.

## Mapping to runtime code

- `Claim` models `ActionLedger.claim_side_effecting()` +
  `LedgerStorage.try_claim_inflight()` CAS ownership/fence acquisition.
- `RecordDecision` models `ActionLedger.record_decision()` as the only allowed
  `INTENDED -> ATTEMPTING` mutation gate.
- `Complete` models `ActionLedger.complete()` on the same fence/owner.
- `Fail` models `ActionLedger.fail(..., failed_after_effect=False)` abort paths.
- `MarkUnknown` models `mark_maybe_crossed()` / fail-after-effect ambiguity.
- `ExpireLease` + `Reclaim` model EXPIRED/not_crossed reclaim after takeover.
- `StaleFenceWrite` models rejected stale-owner CAS writes after takeover.
- `RedispatchUnknown` models fail-closed redispatch on UNKNOWN rows.

## Mapping to verify scenarios

| Scenario | What it proves |
| -------- | -------------- |
| `simulation` | Durable-backend crash windows, fence takeover, effect_id alias dedupe |
| `state-machine-exhaustive` | Deterministic interleavings: matrix, stale-fence, reconcile, concurrent claim |
| `effect-protocol-proof` | **Crash/resume at every scripted step**, alias/fence/UNKNOWN sweeps, enumerated property cases |

Run the deep proof scenario:

```bash
python -m mycelium verify --scenario effect-protocol-proof
```

Pytest entry points:

```bash
pytest tests/test_effect_protocol_proof.py
```

## Optional TLC run (not CI)

1. Install [TLA+ Toolbox](https://lamport.azurewebsites.net/tla/toolbox.html)
   or `tlc2`.
2. Open module `effect_state.tla`.
3. Use model values from `effect_state.cfg` (or set manually):
   - `EffectStates = {"INTENDED","ATTEMPTING","COMMITTED","ABORTED","UNKNOWN"}`
   - `Workers = {"A","B"}`
   - `EffectIds = {"effect-0"}`
4. Check invariants:
   - `AtMostOneCommitted`
   - `UnknownNeverAutoCompletes`

This model is documentation/proof aid only; CI correctness gates remain Python
tests + `mycelium verify` scenarios.

## Proof depth roadmap (not yet exhaustive)

The `effect-protocol-proof` scenario is intentionally finite. Still open for
deeper moat work:

- Real process kill at every await in the ledger wrapper (not just storage
  resume between steps).
- Unbounded two-worker schedule exploration (currently finite scripts + Hypothesis
  sampling over legal prefixes).
- TLC model-checking wired into CI when `tlc2` is available.
- Cross-backend proof runs (file/sqlite/redis/postgres) for the same scripts.
