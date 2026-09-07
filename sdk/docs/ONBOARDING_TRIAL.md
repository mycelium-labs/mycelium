# First external onboarding trial

Use this worksheet with one developer who owns a real tool integration. They
should use the onboarding guide without a maintainer walking them through the
runtime internals.

## Trial setup

Ask the developer to:

1. Start from [`ONBOARDING.md`](ONBOARDING.md) and the local email example.
2. Replace `send_email` with one tool they own.
3. Declare the tool contract, effect class, capability, destination policy, and
   ledger configuration in their own YAML.
4. Run the coverage command and save its JSON output.
5. Preview one allowed and one disallowed action.
6. Execute one permitted action through the existing wrapper.
7. Inspect the recorded transition or outcome and explain the next step.

The trial must use a sandbox or local provider. It must not send real messages,
use production credentials, or call a paid model API.

## Commands to collect

```bash
mycelium coverage --config mycelium.yaml --json > coverage.json
mycelium preview --tool YOUR_TOOL --args-file allowed.json --config mycelium.yaml --json > preview-allowed.json
mycelium preview --tool YOUR_TOOL --args-file denied.json --config mycelium.yaml --json > preview-denied.json
mycelium transitions list --config mycelium.yaml --json > transitions.json
```

Before each preview, record the sandbox provider counter, ledger file digest
or row count, and any configured local grant/budget state. Repeat those checks
after the preview. This establishes only what happened for this configuration
and path; it does not prove every budget or approval implementation path.

## Questions for the developer

- Could you identify which protections were configured versus merely unknown?
- Did the runtime integration status avoid implying complete coverage?
- Did `permitted_at_preview`, `denied`, and `indeterminate` make sense without
  reading the implementation?
- Did you understand that a permitted preview is not authorization or a future
  guarantee?
- Did the denied action expose a useful stable reason without sensitive data?
- Could you execute the action without treating the preview as the result?
- For a completed outcome, could you identify the authoritative record and
  evidence?
- For an uncertain outcome, did reconciliation or operator guidance tell you
  what to do next without suggesting an unsafe blind retry?

## Record findings

```text
Developer / date:
Tool and provider:
Time to first coverage report:
Time to first successful preview:
Time to first execution:

Where they needed help:

What “indeterminate” meant to them:

Was recovery guidance actionable? Why or why not?

Unexpected output or unsafe implication:

Follow-up change requested:
```

Do not treat one successful trial as proof of universal runtime coverage,
exactly-once external execution, semantic correctness, or complete budget and
approval-state verification.
