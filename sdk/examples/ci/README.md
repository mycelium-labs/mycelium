# CI gate template for downstream applications

A copyable GitHub Actions workflow for an application repository that uses
Mycelium. Mycelium's own CI tests the SDK; this template is the other half of
production readiness — a gate in *your* repository that proves your
configuration is wired correctly and that the guarantees hold against
throwaway storage, on every pull request.

This directory is documentation only. Nothing here runs in Mycelium's own CI;
copy the workflow into your repository (for example as
`.github/workflows/mycelium-gate.yml`) and adapt the marked lines.

## What the gate does

1. Installs `mycelium-runtime` and writes a minimal fake-application config
   whose state and ledger live in a fresh SQLite file under the runner's
   temporary directory.
2. Runs Doctor in strict JSON mode
   (`mycelium doctor --config mycelium.yaml --strict --json`).
3. Runs a small, explicitly named Verify scenario set — `redispatch`,
   `contention`, and `worker-crash` — in strict JSON mode. Name a wider or
   narrower set for your application; the supported names are listed by
   `mycelium verify --help`. Do not widen to `all` casually: every scenario
   you name is a guarantee you are asking CI to re-prove on each change.
4. Uploads both JSON reports as workflow artifacts **even when a command
   fails**, so a red run still leaves its diagnostics behind.
5. Fails the job if either command failed. The failure exit status is
   preserved: reports are captured with `tee`, and a final step checks both
   step outcomes instead of letting an early failure — or an
   `if: always()` upload — mask it.

## The workflow

```yaml
name: Mycelium gate

on:
  pull_request:

# Least privilege: the gate reads the checkout and writes nothing back.
# (upload-artifact uses the runtime token internally and needs no
# additional permission.)
permissions:
  contents: read

jobs:
  mycelium-gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1

      - uses: actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0 # v7.1.0
        with:
          python-version: "3.12"

      - name: Install Mycelium
        run: python -m pip install mycelium-runtime

      # A fake application: no tools, no providers, no credentials.
      # State, ledger, and outcome paths are absolute paths inside the runner's
      # disposable temp directory, never your production storage.
      - name: Write the throwaway config
        run: |
          mkdir -p "${{ runner.temp }}/mycelium-ci"
          cat > mycelium.yaml <<YAML
          config_version: 1
          profile: production
          deployment:
            topology: single_node
          transition:
            agent_id: ci-gate-example
            policy_version: '1'
            reclaim_requires_death_signal: true
          state_backend:
            storage: file
            path: ${{ runner.temp }}/mycelium-ci/state.json
          action_ledger:
            storage: sqlite
            path: ${{ runner.temp }}/mycelium-ci/ledger.db
            tools: []
            unclassified_policy: strict
          outcome_emit:
            storage: file
            path: ${{ runner.temp }}/mycelium-ci/outcomes.jsonl
          tools: {}
          YAML

      - name: Doctor (strict, JSON)
        id: doctor
        run: |
          set -o pipefail
          mycelium doctor --config mycelium.yaml --strict --json | tee doctor-report.json

      - name: Verify (strict, JSON)
        id: verify
        if: always()
        run: |
          set -o pipefail
          mycelium verify --config mycelium.yaml --strict --json \
            --scenario redispatch \
            --scenario contention \
            --scenario worker-crash \
            | tee verify-report.json

      - name: Keep the reports
        if: always()
        uses: actions/upload-artifact@cf430e030ddbb5b0abf93d22962f4752f3646cd9 # v7.0.2
        with:
          name: mycelium-gate-reports
          path: |
            doctor-report.json
            verify-report.json
          retention-days: 14

      - name: Preserve the gate's verdict
        if: always()
        run: |
          test "${{ steps.doctor.outcome }}" = "success"
          test "${{ steps.verify.outcome }}" = "success"
```

The action pins above are the full-length SHAs this repository's own
workflows use at the time of writing. Treat them as part of the template:
pin actions by SHA in your repository too, and bump them deliberately.

## Isolation rules

- **Disposable storage only.** The config above points the state backend and
  the action ledger at files under `runner.temp`, which the hosted runner
  discards with the job. Verify exercises the configured backend with
  synthetic operations; if this config pointed at production storage, those
  operations would run against production. Never reuse a production config
  file, production path, or production credentials for this gate.
- **Verify is synthetic.** In normal mode Verify never executes application
  tools, never calls an LLM, and never contacts a provider. It runs Doctor
  first, then the named scenarios against the configured backend. The
  optional cluster mode is deliberately not used here: it requires an
  explicitly configured sandbox (`verify.cluster.enabled: true`).
- **Doctor is read-only here.** The gate does not pass `--fix`, so Doctor
  only reports. Run `--fix` locally, review the diff, and commit it yourself
  if a report asks for metadata changes.

## What a green run does not prove

Passing synthetic scenarios is not provider attestation, and it is not proof
that every application call is instrumented. Doctor checks that protections
are configured and detectably wired; Verify re-proves the named guarantees
against throwaway storage. Coverage of your real call sites is a separate
question — see `mycelium coverage` and the
[onboarding guide](../../docs/ONBOARDING.md).
