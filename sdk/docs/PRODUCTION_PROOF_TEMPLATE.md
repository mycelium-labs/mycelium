# Production-proof evidence template

Use this worksheet only when a **consenting deployment owner** wants to publish
verifiable evidence from a **real consequential workflow** in production.
Leave every result field blank until that owner fills them from their own
systems.

This template is **not** the onboarding trial. Sandbox and synthetic checks
belong in [`ONBOARDING_TRIAL.md`](ONBOARDING_TRIAL.md) and must not be rewritten
as production claims. See residual risks in
[`FAILURE_AND_THREAT_MODEL.md`](FAILURE_AND_THREAT_MODEL.md) before asserting
what a run proved.

Do **not** invent a before baseline. Do **not** treat missing telemetry as zero
incidents. Do **not** run a consequential action, contact customers, or fill
results on behalf of a deployment you do not operate.

**Publication gate:** obtain the deployment owner's explicit permission before
publishing any name, logo, or operational details (topology, providers,
customer-facing workflows, or identifying logs).

---

## 1. Observation window and scope

```text
Observation start (UTC):
Observation end (UTC):

Workflow name / boundary (what is in scope):
Workflow boundary (what is explicitly out of scope):

SDK revision (package version or git SHA):
Config revision (file digest, commit, or config-id):

Backend / topology (storage class, single- vs multi-node, region class only):
Stable identity strategy (how request_id / effect_id / provider keys are minted):
```

---

## 2. Source-backed counts

Record each count with its **source** (query, dashboard, ledger export, or
“not available”). If data is missing, write `missing` — never `0` by default.

```text
Protected actions attempted:
  count:
  source:
  missing? (yes/no + reason):

Duplicate / redispatch attempts observed:
  count:
  source:
  missing?:

Observed external effects (provider-side):
  count:
  source:
  missing?:

Blocks / hard refusals:
  count:
  source:
  missing?:

Unresolved / indeterminate outcomes:
  count:
  source:
  missing?:
```

---

## 3. Evidence classes (keep separate)

Fill only the class that applies. Do not merge rows across classes.

### A. Observed production evidence

Facts from the live deployment during the observation window (ledger rows,
provider receipts, outcome telemetry). No synthetic fault injection here.

```text
What was observed:

Authoritative records / links (sanitized):

What this does *not* prove:
```

### B. Synthetic verification

Sandbox, `mycelium verify`, cluster verification, or other non-production
exercises. Label them synthetic; they do not become production proof by
proximity.

```text
Scenarios / commands run:

Report links (sanitized):

Explicitly not claimed about production:
```

### C. Operator assertions

Statements the operator believes but cannot back with the sources above
(for example Redis persistence policy, signing-key custody, or call-site
discipline outside Mycelium wrappers).

```text
Assertion:

Why it is an assertion (not observed evidence):

How a reader should treat it:
```

---

## 4. Sanitized proof artifacts

Placeholders only — leave blank until the owner attaches redacted exports.

```text
Sanitized proof report URL or path:
Ledger / outcome export (redacted) URL or path:
Provider receipt samples (redacted) URL or path:
Config digest / attestation binder URL or path:
```

---

## 5. Operator recovery rehearsal

Record whether operators practiced recovery **without** treating rehearsal as
a production incident count.

```text
Rehearsal date (UTC):
Scenario rehearsed (release, reconcile, reclaim, etc.):
Environment (staging / production read-path / other):
Outcome (what operators did; no customer identifiers):
Gaps found:
Link to rehearsal notes (sanitized):
```

---

## 6. Residual risks and honest limits

List risks that remain after this observation. Prefer citing
[`FAILURE_AND_THREAT_MODEL.md`](FAILURE_AND_THREAT_MODEL.md) § F rather than
claiming they were eliminated.

```text
Residual risks still in force:

Claims explicitly *not* made by this report:

Deployment-owner permission to publish name/logo/ops detail (yes/no + date):
Publisher contact (optional, owner-approved):
```

---

A completed copy of this template is evidence for **one** workflow, window, and
configuration. It does not prove universal runtime coverage, exactly-once
external execution under every failure mode, or that missing telemetry means
zero incidents.
