# Developer onboarding: inspect, preview, execute, explain

Mycelium protects a tool action across its lifecycle. Start with configured
metadata, preview an input without invoking the tool, execute through the
runtime wrapper, then inspect the durable transition or outcome.

```bash
mycelium coverage --config mycelium.yaml
mycelium preview --tool send_email --args-file email.json --config mycelium.yaml
mycelium run --config mycelium.yaml -- python -m your_agent
mycelium transitions list --config mycelium.yaml
```

`coverage` separates configured protections from runtime verification. A
missing association is `unknown`; static detection never proves every call uses
the wrapper. `preview` uses only the preflight loader and declarative contract
and destination validators. It never imports customer callables, invokes a
provider, creates a ledger row, consumes grants, reserves budgets, resolves
credentials, or runs reconciliation. Its outcome is always `not_executed`.

`denied` means an evaluated required check failed. `indeterminate` means no
denial is known but execution-only checks remain. `permitted_at_preview` means
the applicable preview checks passed for the observed snapshot. Neither result
is authorization or a reservation; execution re-evaluates controls at the
shared atomic boundary. `UNKNOWN` means the effect may have happened and must
follow read-only reconciliation or operator resolution. It is not safe-to-retry
guidance.

See the complete deterministic
[`langgraph_email_onboarding`](../examples/langgraph_email_onboarding/README.md)
example. A real integration must supply host-owned stable action identity,
capability, authorized destinations, policy, and provider idempotency or
reconciliation support. Direct provider calls bypass Mycelium coverage.

The [discovery and instrumentation design spike](DISCOVERY_AND_INSTRUMENTATION_DESIGN.md)
shows how a reviewed Python callable can be instrumented without changing its
application body, and records the coverage limits of that approach.

To re-check this wiring on every change, copy the
[downstream CI gate template](../examples/ci/README.md): a GitHub Actions
workflow that runs Doctor and a small named Verify scenario set against
throwaway SQLite storage in your own repository, and keeps the JSON reports
as artifacts when a check fails.

## Report consumer guide: Doctor and Verify JSON

Doctor and Verify already emit structured JSON (`--json`). A CI consumer should
read the fields below rather than importing the dataclasses. Command usage and
scenario lists stay in the SDK README
([`mycelium doctor`](../README.md#mycelium-doctor-verify-protection-is-real),
[`mycelium verify`](../README.md#mycelium-verify-exercise-the-guarantees));
this section is only the report contract.

Three layers stay distinct:

| Layer | What a green signal means | What it does not mean |
| --- | --- | --- |
| Doctor check / `production_ready` | Configured readiness for the check's named `evidence` kind | Every application call site is wrapped, or a provider outcome is correct |
| `protection_coverage` | Declared controls plus optional host-supplied wrapper markers | Hidden call sites, provider-boundary placement, or recovery paths |
| Verify scenario / `empirically_verified` | Named synthetic scenario(s) passed in an isolated namespace | Real application tools ran, or a live business provider is certified |

A `SKIP` is never proof. Treat skipped scenarios and `not_verifiable` /
`operator_asserted` Doctor evidence as uncovered, not green.

### Doctor JSON (`DoctorReport.to_dict()`)

Top-level fields:

| Field | Meaning |
| --- | --- |
| `overall_status` | `PASS`, `WARN`, `FAIL`, or `SKIP` (aggregate over checks) |
| `profile` | Loaded config profile |
| `checks` | Per-check objects (see below) |
| `pass_count` / `warning_count` / `failure_count` / `skipped_count` | Tallies |
| `production_ready` | `true` only when there are no `FAIL` checks and `profile` is `production` |
| `distributed_ready` | `true` only for `deployment.topology: multi_node` with no `FAIL` and without a `topology.omitted` warning |
| `load_error` | Present when YAML/env load failed; otherwise `null` |
| `protection_coverage` | Per-tool inventory of configured controls vs observed wrappers |

Each check object (`DoctorCheck.to_dict()`):

```json
{
  "id": "redis.persistence",
  "category": "Storage",
  "status": "WARN",
  "summary": "Redis persistence is operator-asserted",
  "details": "AOF/RDB settings are not inspected by Mycelium.",
  "remediation": "Verify durability for your Redis topology and accepted loss window.",
  "evidence": "operator_asserted",
  "blocking": false
}
```

Evidence kinds (exact `evidence` string values):

| `evidence` | How the conclusion was established |
| --- | --- |
| `statically_verified` | Declared configuration / schema inspection |
| `runtime_registration_verified` | Detectable registration or wrapper markers |
| `connectivity_verified` | Safe backend probe (for example a `PING`) |
| `operator_asserted` | Outside Mycelium's inspectable evidence (for example Redis AOF) |
| `not_verifiable` | Cannot be proven from config alone (for example hidden call-site binding) |

Sanitized excerpt of a passing Doctor report (fields abbreviated):

```json
{
  "overall_status": "PASS",
  "profile": "production",
  "pass_count": 12,
  "warning_count": 0,
  "failure_count": 0,
  "skipped_count": 1,
  "production_ready": true,
  "distributed_ready": false,
  "load_error": null,
  "checks": [
    {
      "id": "ledger.backend",
      "category": "ActionLedger",
      "status": "PASS",
      "summary": "Action ledger storage is durable",
      "details": "",
      "remediation": "",
      "evidence": "statically_verified",
      "blocking": true
    }
  ],
  "protection_coverage": [
    {
      "tool": "send_email",
      "configured": true,
      "configured_controls": ["ledger", "entity_guard"],
      "wrapper_status": "unverified",
      "observed_wrapper_markers": [],
      "side_effect_class": "write_external",
      "ledger_storage": "sqlite",
      "durable_ledger_configured": true,
      "request_identity": "host_field_configured",
      "provider_boundary": "unverified",
      "recovery_path": "unverified",
      "missing_requirements": [
        "wrapper_observation",
        "provider_boundary_verification",
        "recovery_path_verification"
      ]
    }
  ]
}
```

`wrapper_status: "unverified"` with `provider_boundary: "unverified"` is expected
from the CLI: Doctor did not receive host callables. That is configuration
readiness, not application coverage. Supplying `observed_tools` to
`run_doctor_on_config` can mark wrappers as observed; it still does not prove
provider outcomes.

### Verify JSON (`VerificationReport.to_dict()`)

Verify runs Doctor first, isolates a synthetic namespace, then executes the
named scenarios. Nested `doctor` is Doctor's full `to_dict()` payload.

| Field | Meaning |
| --- | --- |
| `overall_status` | Aggregate: `PASS`, `WARN`, `FAIL`, `SKIP`, or `ERROR` |
| `isolation_status` | Namespace isolation result (`PASS` / `WARN` / `FAIL` / `SKIP` / `ERROR`) |
| `isolation_detail` | Human detail (namespace prefix, single-node labels, refusal reason) |
| `refused` | `true` when safe isolation could not be proven; scenarios must not be trusted |
| `production_ready` | `true` only when Doctor was production-ready, every selected scenario passed, isolation stayed `PASS`, and cleanup succeeded |
| `empirically_verified` | `true` only when every selected scenario has `status: "PASS"` |
| `scenarios` | Per-scenario evidence objects |
| `framework_error` | Verifier/setup error (unknown scenario name, load failure); not a scenario result |
| `pass_count` / `warning_count` / `failure_count` / `skipped_count` / `error_count` | Tallies |

Each scenario object (`VerificationEvidence.to_dict()`) includes `status`,
`summary`, `expected_behavior`, `observed_behavior`, `limitations`,
`remediation`, `body_executions`, `ledger_decisions`, and related counters.
`limitations` records what the pass does not prove (for example
`"synthetic provider only; does not prove a real business provider"`).

Passing scenario excerpt:

```json
{
  "overall_status": "PASS",
  "backend": "SQLite",
  "isolation_status": "PASS",
  "isolation_detail": "namespace=mycelium:verify:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee: topology=single_node (single-node verification only)",
  "refused": false,
  "production_ready": false,
  "empirically_verified": true,
  "pass_count": 1,
  "skipped_count": 0,
  "scenarios": [
    {
      "scenario": "redispatch",
      "backend": "sqlite",
      "namespace": "mycelium:verify:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee:",
      "attempts": 2,
      "body_executions": 1,
      "status": "PASS",
      "summary": "redispatch held under reconstruct",
      "expected_behavior": "exactly one body execution",
      "observed_behavior": "body executed once; peer redispatched",
      "limitations": [],
      "remediation": ""
    }
  ]
}
```

`empirically_verified: true` with `production_ready: false` is normal on a
development profile: synthetic guarantees held, but Doctor did not mark the
deployment production-ready.

Refused or skipped excerpt (do not treat as proof):

```json
{
  "overall_status": "ERROR",
  "backend": "unknown",
  "isolation_status": "FAIL",
  "isolation_detail": "unknown action_ledger storage cannot prove isolation",
  "refused": true,
  "production_ready": false,
  "empirically_verified": false,
  "scenarios": [],
  "framework_error": null
}
```

```json
{
  "overall_status": "WARN",
  "backend": "memory",
  "isolation_status": "PASS",
  "refused": false,
  "production_ready": false,
  "empirically_verified": false,
  "scenarios": [
    {
      "scenario": "contention",
      "backend": "memory",
      "status": "SKIP",
      "summary": "multiprocess contention not proven on memory",
      "limitations": [
        "memory is process-local; multiprocess contention not proven"
      ],
      "remediation": "Use a durable shared backend for contention."
    }
  ]
}
```

When `refused` is `true`, exit code is `3` and no scenario list should be read
as success. When a scenario is `SKIP`, `empirically_verified` stays `false`
even if other scenarios passed.

### Strict mode and exit codes

Both commands accept `--strict` (warnings become exit `1`) and `--json`
(stdout is `to_dict()` output, keys sorted, secrets sanitized).

Doctor (`exit_code_for_report`):

| Exit | When |
| --- | --- |
| `0` | No load error, no failures; warnings allowed unless `--strict` |
| `1` | One or more `FAIL` checks, or `--strict` with warnings |
| `2` | Config could not be loaded (`load_error` set) |

Verify (`exit_code_for_verify`):

| Exit | When |
| --- | --- |
| `0` | Isolation allowed the run and every selected scenario is `PASS` (warnings only fail under `--strict`) |
| `1` | Scenario `FAIL`/`ERROR`/`SKIP` (or mixed non-`PASS`), blocking Doctor findings, or `--strict` with warnings |
| `2` | Framework/setup error (including Doctor `load_error` or unknown scenario) |
| `3` | Isolation refused (`refused: true`) |

Exit `0` on Verify still does not certify a provider. Pair green Verify with
application coverage and provider tests; see the
[evidence interpretation table](ARCHITECTURE_AND_GUARANTEE_MAP.md#evidence-interpretation)
for the broader map.
