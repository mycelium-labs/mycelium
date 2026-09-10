---
name: mycelium-setup
description: Set up or repair Mycelium in an existing agent project, including Python, TypeScript, Go, and CRM agents, by inspecting the application, selecting the supported integration path, wiring real tool boundaries, and verifying the integration. Use when the user asks to install, configure, integrate, or fully wire Mycelium. Do not use for unrelated application work or ordinary Mycelium SDK development.
---

# Set Up Mycelium

Complete the integration in the target project rather than only explaining it.
Inspect first, make the smallest safe changes, and leave the project runnable.

## Sources of truth

- Follow the target repository's `AGENTS.md` and preserve unrelated changes.
- Use the installed Mycelium version's CLI, generated full template, and README
  as the configuration/API authority. Do not rely on remembered field names.
- If the Mycelium source checkout and `graphify-out/graph.json` are present, use
  its scoped Graphify query flow before broad source inspection.

Generate a reference configuration in a temporary directory with the installed
CLI (`mycelium init --full`), and inspect `mycelium --help`, `mycelium doctor
--help`, and `mycelium verify --help`. Never overwrite the application's current
configuration merely to obtain a template.

## Runtime integration path

Detect the application's language before choosing setup steps:

- **Python:** use the in-process runtime, `mycelium.yaml`, supported framework
  integration, decorators, or the manual API.
- **TypeScript or Go:** use the published experimental client with a local
  Python sidecar speaking `v1alpha1`.
- **Another language:** use the same authenticated HTTP/OpenAPI contract
  directly.

For a non-Python project, do not port or recreate Mycelium's state machine.
Configure the local sidecar with one tenant/application, owner-only bearer-token
file, absolute ledger/outcome paths, and an explicit loopback port. Wire the host
to follow claim → provider boundary → complete/fail and to treat every
non-`EXECUTE` disposition as “do not call the provider.” State clearly that the
current sidecar is experimental, trusted-local, and not approved for remote or
multi-tenant production use.

## Configuration completion contract

Treat `mycelium.yaml` as an evidence-backed application configuration, not a
generic scaffold. Fill every value that can be proven from source code,
deployment files, existing configuration, and declared environment-variable
names. Remove template examples and TODOs that do not describe a real callable.

For CRM and similar business agents, explicitly look for customer/contact reads,
record creation or updates, email/message sends, imports/exports, bulk actions,
deletes, payments, webhooks, scheduled jobs, and external API mutations. Bind
each reachable operation to its actual callable and classify its externally
observable effect. Do not assume a function is safe because its name sounds
read-only.

Do not stop after generating YAML. Completion requires a loadable configuration,
runtime wiring at the actual execution boundary, and behavioral verification.
If a host-owned value cannot be inferred safely, leave one precise documented
placeholder that fails closed and include it in the final questions list. Do not
leave vague TODOs such as "configure this later."

## Feature applicability

Mycelium makes tool actions reliable across their full lifecycle. Do not reduce
an integration to duplicate prevention or the action ledger alone. Inspect
pre-execution validation and authority, run-level controls, and post-attempt
outcome/recovery needs for every reachable tool. The ledger is a common
production foundation and default on-ramp, not the whole product.

Do not enable every available Mycelium feature. Enable every feature that
applies and is genuinely wired. An omitted inapplicable feature is not an
integration failure.

Classify each feature before editing configuration:

| Category | Typical features | Decision rule |
| --- | --- | --- |
| Production foundation | Action ledger, outcomes, durable storage, explicit request IDs, secret protection | Usually required for consequential production tools. |
| Applicable guardrails | Loop, scope, budget | Enable when the runtime exposes stable run identity and the relevant protected boundaries. |
| Capability-dependent | Destructive confirmation, authority windows | Enable only when destructive or time-authorized operations exist. |
| Host-integrated | State authority, use-time currency, completion | Enable only when real validators, canonical state references, or terminal hooks can be wired. |
| Operator-provisioned | Signed audit receipts | Enable only with a genuine operator-controlled signing key. |
| Framework-specific | LangGraph integration, history and message guards | Enable only for a supported and observable framework path. |
| Inapplicable | A refund guard when the agent has no refund operation | Leave disabled intentionally and explain why. |

Do not call a host-integrated or operator-provisioned feature enabled merely
because its YAML section exists. If its trusted callable, terminal boundary,
authority, or secret is missing, classify it as deferred and keep it fail closed.

## Workflow

1. Inspect the dependency files, runtime entry points, framework, tool/task
   registration, existing retry behavior, provider clients, deployment files,
   environment-variable names, and current Mycelium configuration/wrappers.
2. Inventory every callable reachable as an agent tool or durable task. Trace
   aliases, registries, dynamic exports, and decorators so the same callable is
   not omitted or wrapped twice. Record the inventory while working: tool name,
   callable path, provider/effect, class, business identity source, and selected
   guard/storage path.
3. Classify each tool from code evidence. Read
   [references/tool-classification.md](references/tool-classification.md) before
   writing configuration. If the trusted host selects exact write destinations
   per run, read
   [references/dynamic-destination-authority.md](references/dynamic-destination-authority.md)
   before declaring that no truthful static entity allowlist exists.
4. Add the appropriate integration dependencies. Python projects install
   `mycelium-runtime` and relevant extras. TypeScript projects use
   `@mycelium-labs/sidecar-client@experimental`; Go projects use
   `github.com/mycelium-labs/mycelium/clients/go@v0.1.1`. Non-Python paths also
   need the Python sidecar as a separately managed local process. Preserve
   existing version policy. Do not bump the application's version.
5. For in-process Python, create or merge `mycelium.yaml`. For a non-Python
   project, create or merge the separate `kind: mycelium-sidecar` development
   configuration instead. Preserve deliberate existing settings.
   Record the applicability decision for each meaningful feature: enabled,
   deferred for host work, deferred for operator input, or not applicable.
   Prefer current template defaults, explicit callable paths, stable transition
   identity, strict policies for consequential tools, and durable storage that
   matches the discovered deployment topology. Load the completed file through
   the installed Mycelium version before treating it as valid. Use YAML entity
   allowlists only for destinations known ahead of time. For host-selected
   destinations, construct one immutable `EntityGuardPolicy` from the trusted
   selection for that run and compose it outside the ledgered tool with
   `apply_decision_policy`; never let model output create or widen that policy.
6. Wire the real execution boundary. Prefer a supported framework integration
   or Mycelium's wrapper/config instrumentation path. For a custom loop, wrap the
   callable at the last boundary before execution. Ensure sync/async parity and
   idempotent installation. Merely creating YAML is not completion.
7. If stateful guards are enabled, configure one `state_backend`; use file only
   for a confirmed single-node deployment and Redis/Postgres for multiple
   workers. If legacy guard state exists, run the migration plan only. Applying
   a production migration requires explicit authorization and stopped workers.
8. When a consequential provider operation can become ambiguous, default to a
   hard block. Only add automatic reconciliation when the provider has a
   genuinely read-only lookup path and a stable external operation handle. Read
   [references/provider-reconciliation.md](references/provider-reconciliation.md)
   before creating an adapter.
9. Add behavioral tests proving the wrapper executes at the actual boundary,
   redispatch does not silently duplicate effects, missing identity fails as
   configured, and framework terminal/LLM hooks are active when selected.
10. Run focused application tests, configuration import/load, `mycelium doctor`,
    `mycelium doctor --strict`, and appropriate synthetic `mycelium verify`
    scenarios. Run the project's normal lint/test commands afterward. Fix
    integration failures before handoff. If strict Doctor cannot pass because a
    host-owned input is absent, preserve the fail-closed configuration and report
    the exact missing input instead of weakening the policy.

## Host-owned questions

Infer first; ask only for decisions or authority the repository cannot prove.
Group unresolved items into a short final list, such as:

- the stable business operation ID used across retries;
- production Redis/Postgres connection environment-variable name or topology;
- provider idempotency-key and read-only reconciliation guarantees;
- approved CRM destinations, or the trusted host selection/approval path that
  will issue exact per-run destination policy;
- destructive objects or authority windows;
- secret/signing-key environment-variable names and production permissions.

Never ask the user to transcribe information already present in the repository.

## Safe autonomy

Do all work supported by repository evidence without asking the developer to
transcribe configuration or wire wrappers manually. Make these boundaries
non-negotiable:

- Never invent a secret, production DSN, business request identity, approved
  destination, destructive authority, signing authority, or provider fact.
- Reuse environment-variable names already declared by the project; otherwise
  add a documented placeholder name, never a value.
- Never weaken a policy merely to make Doctor green. If stable business identity
  cannot be inferred, configure the path to fail closed and report the precise
  unresolved field.
- Never claim `NOT_EXECUTED` from absence, timeout, indexing lag, duplicate
  matches, or malformed provider data.
- Never call a live business tool/provider, alter production data, apply a
  migration, release a blocked transition, push, publish, or deploy unless the
  user separately authorizes that action.
- A synthetic verification report does not prove live provider permissions.
  Production reconciliation credentials still need read-only scopes.

## Completion report

Return a short handoff containing:

- the completed `mycelium.yaml` path and any intentionally unresolved placeholders;
- files and execution boundaries wired;
- tool classifications and identity sources chosen;
- destination authority source (static host allowlist or exact per-run host policy);
- storage/topology choice;
- Doctor, Verify, and application-test results;
- only the fail-closed host-owned inputs the application owner must supply.

Finish with an applicability report grouped as:

- **Enabled** — configured, genuinely wired, and verified features;
- **Deferred — host work required** — features needing application validators,
  canonical state, stable identity, or terminal integration;
- **Deferred — operator input required** — features needing operator-controlled
  secrets, signing keys, permissions, or explicit authority;
- **Not applicable** — features that do not match the agent's tools, framework,
  deployment, or authority model.

Give one short reason for every deferred or inapplicable item. Do not list a
feature as enabled when only a template section or placeholder exists.

Do not describe the setup as complete if YAML exists but the runtime boundary is
unwired, tests fail, or a consequential tool lacks trustworthy identity.

## Composite recovery

Use composite recovery only for sequential functions whose consequential calls
already pass through the actual Mycelium `ledger`/`ledger_sync` wrappers. Keep
the original function body unchanged and do not claim a decorator discovers
hidden provider calls.

```python
from mycelium import composite

@composite(
    storage=ledger_storage,
    operation_id_from=lambda _args, kwargs: kwargs["job_id"],
)
def publish_change(job_id: str):
    commit = create_commit(idempotency_key=f"{job_id}:commit")
    pushed = push_branch(idempotency_key=f"{job_id}:push")
    return update_tracking_record(
        idempotency_key=f"{job_id}:tracking", pushed=pushed
    )
```

Verify child wrappers and bindings first. Use `register_composite_boundary` for
aliases or bound callables that static inspection cannot resolve. A genuinely
local deterministic helper can use `register_composite_helper`; unresolved
calls are rejected rather than silently omitted. Configure a
durable SQLite or file backend, derive the operation ID from the host request
or job, and preserve it across retries. Inspect
`function._mycelium_composite_manifest` and resolve diagnostics before any
live provider call. Never use a fresh random ID per retry or in-memory storage
as restart evidence.

Parent leases renew automatically while the body runs and fail closed if
renewal is lost. The supported syntax is deliberately narrow: assignments,
expression statements, and one final return with each supported call at a
statement boundary. Conditional/short-circuit expressions, comprehensions,
generators, nested calls, early returns, loops, nested definitions, nested
composites, and opaque consequential boundaries are rejected before effects.
Completed children replay serialized results; admission alone cannot mark a
parent complete, and ambiguous children still reconcile or hard-block.
Provider idempotency or a truthful reconciliation adapter remains necessary
when a request may have crossed the provider boundary.
