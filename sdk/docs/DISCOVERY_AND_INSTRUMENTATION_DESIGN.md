# Discovery and instrumentation design spike

Issue #116 asks how an existing Python application can put observable tool calls
through Mycelium without rewriting its function bodies. This spike uses the
current SDK's static `init --detect` scanner, explicit callable configuration,
and `mycelium run` launcher. The runnable fixture includes an
[unchanged application body](../tests/fixtures/discovery_spike/app.py).
Its [inventory](../tests/fixtures/discovery_spike/inventory.json) records what
the spike can and cannot cover.

## Chosen first boundary

Start with **registered top-level Python callables**. Static inspection can
find `@tool` functions without importing application code. A reviewer then
assigns a stable logical tool name, side-effect class, destination rules,
request identity source, and recovery capability in YAML. No classification
is inferred from the function name. `mycelium run` imports the configured
callables at process startup and replaces their module attributes with the
real `config.apply_tool` wrapper. The application body stays unchanged.

This is a hybrid workflow: static discovery proposes candidates; explicit
configuration authorizes instrumentation; runtime evidence establishes which
calls actually crossed a wrapper. Static configuration alone is never proof of
complete call-site coverage.

## Identity, overlap, and failure behavior

- The configured logical tool name, callable path, and declared policy define
  the boundary.
  The host must supply a stable `request_id` for consequential calls. The
  ledger derives its effect identity from the request, tool, canonical inputs,
  and destination. A detector cannot invent a business request identity.
- Repeating startup instrumentation on the same logical tool is idempotent:
  the config-applied marker returns the existing wrapper. A conflicting
  logical tool/task name or a partially applied standalone guard raises
  `ConfigError` rather than stacking a second wrapper.
- A configured target that cannot be imported or is not callable fails the
  launcher before the application begins. An observed but unclassified tool
  receives no protection until the reviewer adds it to YAML. Production
  policy can reject unclassified calls only at an observed Mycelium boundary;
  an unregistered direct provider call remains outside that boundary.
- If framework hooks and callable patching both point at one function, the
  integration owner must select one wrapping path. Future manifest entries
  should carry the chosen owner and a wrapper identity; the existing marker
  detects overlap at the callable but cannot discover hidden inner effects.

## Reviewable inventory

The fixture has four deliberately different findings: `protected` (one
runtime-observed wrapper), `unclassified` (detected but not configured),
`unsupported` (a class method outside the current detector/launcher path),
and `opaque` (a direct call identified by manual review). The inventory names
its evidence and limitation. It never turns an unknown number of hidden calls
into a false zero.

To reproduce the protected finding from `sdk/`:

```bash
pytest tests/test_discovery_spike.py
```

The test copies the existing application unchanged, launches it through
`mycelium run`, and checks that two calls with one request identity execute
the body once and return the same stored result. It also checks detector
findings and repeated instrumentation. The fixture uses process-local memory
storage for this bounded demonstration; production recovery requires a durable
backend and provider-specific idempotency or reconciliation.

## Integration path and later work

`init --detect` should surface candidates with an `unclassified` state before
generating conservative YAML; it must not mark them protected. Doctor and
`mycelium coverage` should join configuration with observed runtime evidence
and preserve `unknown` when no such evidence exists. The spike inventory is a
review artifact for that future workflow, not a production coverage claim.

Later work can add framework registries, class methods, provider adapters,
and runtime coverage receipts. Each new surface needs an explicit
instrumentation owner, verified stable identity, and overlap tests. Network
tracing or uncontrolled egress is outside this design: Mycelium cannot infer
the business meaning or authorization of arbitrary outbound requests.
