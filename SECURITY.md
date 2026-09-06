# Security policy

Mycelium is a reliability and safety boundary around **consequential tool
actions**: validation and authority before execution, runtime control, and
outcome evidence or recovery afterward. Payments, outbound email, API
mutations, and similar side effects are examples. Security-sensitive reports
deserve a **private** path — not a public issue comment thread.

## Supported versions

Security fixes ship on the **latest published** [`mycelium-runtime`](https://pypi.org/project/mycelium-runtime/)
release (see PyPI for the current version).

| Status | Policy |
|--------|--------|
| **Latest PyPI release** | Receives security fixes as PATCH releases when applicable. |
| **Previous minor line** | May receive backported fixes at maintainer discretion; ask if you are pinned. |
| **Older releases** | Unsupported — upgrade to the latest release before requesting a fix. |

Release cadence and semver rules: [`sdk/docs/RELEASE.md`](sdk/docs/RELEASE.md).
Security issues may justify an out-of-band PATCH even when the normal batch
cadence would wait.

## Reporting a vulnerability

**Do not** open a public GitHub issue for an exploitable or suspected
exploitable vulnerability.

### Preferred: GitHub private vulnerability reporting

Use the repository's private advisory form (maintainers only see the report
until disclosure is coordinated):

**https://github.com/mycelium-labs/mycelium/security/advisories/new**

This is the preferred channel. It keeps reproduction details, credentials, and
customer impact out of public view.

### What to include

Please provide as much of the following as you can:

1. **Summary** — what can go wrong, and under what configuration (YAML profile,
   storage backend, side-effect class, guard modules enabled).
2. **Impact** — duplicate side effects, bypass of a guard, data exposure,
   privilege escalation, or denial of service.
3. **Reproduction** — minimal steps, version or commit, and synthetic fixtures
   only (see below).
4. **Proof-of-concept** — if you have one, attach it to the private advisory
   rather than pasting it in a public forum.

### What not to include

- Live production credentials, API keys, or session tokens
- Real customer data, payment instruments, or PII from production systems
- Unredacted logs from production deployments

Use `mycelium verify` scenarios, in-memory/file ledger storage, and synthetic
destinations when demonstrating a boundary failure. See
[`sdk/docs/FAILURE_AND_THREAT_MODEL.md`](sdk/docs/FAILURE_AND_THREAT_MODEL.md)
for scope boundaries.

## Response expectations

| Milestone | Target |
|-----------|--------|
| **Initial acknowledgement** | Within **3 business days** of a well-formed private report. |
| **Triage / severity assessment** | Within **10 business days** when possible. |
| **Fix or mitigation plan** | Communicated in the private advisory thread; timeline depends on severity and complexity. |

We may ask clarifying questions. Silence on a public issue does not mean a
report was ignored — use the private advisory path above.

## Coordinated disclosure

We prefer coordinated disclosure:

1. Report privately and allow reasonable time to investigate and ship a fix.
2. Do not disclose exploit details publicly until we agree on timing, or until
   **90 days** have passed with no good-faith progress (whichever comes first,
   unless a shorter window is required to protect users).
3. We will credit reporters who wish to be named when we publish an advisory;
   tell us your preference.

## Operator actions are not authenticated via GitHub

Mycelium's CLI supports operator workflows such as
`mycelium transitions release` (documented in the SDK README). The `--by`
field and similar audit stamps record **who the host asserts** performed an
action; they are **not** an authentication or authorization mechanism.

**GitHub issue comments, PR reviews, and discussion threads are not a valid
channel to authorize operator release, destructive grants, or production
configuration changes.** Attackers must not be able to social-engineer a
release by commenting on a public thread. Hosts must enforce release authority
through their own identity, access control, and runbooks.

If you find a way to bypass ledger gates, reconciliation, or guard predicates
without proper host authorization, report it privately using the advisory form
above.

Provider reconcilers are security-sensitive because `NOT_EXECUTED` permits one
more attempt. Before shipping an adapter, run `mycelium providers verify` and
retain its signed, source-bound report. That report verifies the synthetic
adapter cases only; it does not authenticate the deployed provider account or
prove that its credentials have read-only scopes. Enforce those scopes in the
provider and deployment configuration.

## Security-related documentation

- Failure and threat model (ledger core guarantees and limits):
  [`sdk/docs/FAILURE_AND_THREAT_MODEL.md`](sdk/docs/FAILURE_AND_THREAT_MODEL.md)
- Provider conformance commands and report limitations:
  [`sdk/README.md#provider-adapter-conformance-and-signed-reports`](sdk/README.md#provider-adapter-conformance-and-signed-reports)
- Release and hotfix policy:
  [`sdk/docs/RELEASE.md`](sdk/docs/RELEASE.md)

## Questions that are not vulnerabilities

Configuration questions, feature requests, and general reliability discussions
belong in [GitHub Discussions](https://github.com/mycelium-labs/mycelium/discussions)
or a public issue when no exploit is involved. When in doubt, start with a
private advisory — we can always reclassify a report as a bug or docs fix.
