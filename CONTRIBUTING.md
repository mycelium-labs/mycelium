# Contributing to Mycelium

Thank you for helping improve Mycelium.

Mycelium sits on the execution boundary for payments, outbound messages, API
mutations, and other consequential agent actions. A small behavioral change can
alter whether an operation runs, retries, returns cached evidence, or fails
closed. Contributions are welcome, but correctness and clear evidence matter
more than patch volume.

## Before you start

- Search [open issues](https://github.com/mycelium-labs/mycelium/issues) and
  pull requests before starting work.
- For a bug, include a minimal reproduction and the affected Mycelium version,
  Python version, framework, storage backend, and deployment topology when
  relevant.
- For a substantial API, configuration, storage, or guarantee change, open an
  issue first so the behavior and compatibility expectations can be agreed on.
- Keep one pull request focused on one logical problem. Unrelated cleanup makes
  reliability changes harder to review.
- Never put credentials, customer data, payment details, private logs, or other
  sensitive material in an issue, test fixture, commit, or pull request.

If the report may describe an exploitable vulnerability or a bypass of a
runtime safety boundary, do not open a public issue. Follow
[SECURITY.md](SECURITY.md) and use
[GitHub private vulnerability reporting](https://github.com/mycelium-labs/mycelium/security/advisories/new).

## Ways to contribute

Useful contributions include:

- Reproducible bug fixes with regression tests
- Reliability and concurrency proofs
- Storage-backend correctness and parity improvements
- Framework integration fixes
- Documentation corrections grounded in shipped behavior
- Focused test coverage for an existing guarantee or residual risk
- Small usability improvements with a clear user outcome

Please avoid speculative abstractions, large generated rewrites, duplicate
issues, or features without a concrete use case. A pull request should solve a
demonstrated problem, not create work for maintainers to discover what changed.

## Development setup

The distributable Python package lives in `sdk/`. Python 3.10 through 3.13 is
tested in CI.

### Preferred: uv

```bash
git clone https://github.com/mycelium-labs/mycelium.git
cd mycelium/sdk
uv sync --extra dev --extra redis --extra postgres
uv run pytest tests/ -v
uv run ruff check mycelium tests
uv run pyright
```

### Alternative: pip

```bash
git clone https://github.com/mycelium-labs/mycelium.git
cd mycelium/sdk
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev,redis,postgres]"
pytest tests/ -v
ruff check mycelium tests
pyright
```

On Windows PowerShell, activate the environment with
`.venv\Scripts\Activate.ps1`.

## Run the right tests

At minimum, run the focused tests for your change and the standard local
checks:

```bash
cd sdk
uv run pytest tests/ -v
uv run ruff check mycelium tests
uv run pyright
```

Use the equivalent commands inside an activated pip environment if you are not
using uv.

Additional expectations depend on what changed:

- **Bug fix:** add a regression test that fails before the fix and passes after
  it.
- **Public API or durable state:** test round trips, legacy data, defaults, and
  backward compatibility.
- **Ledger or transition behavior:** test the allowed path and the fail-closed
  path, including stale owner/fence behavior when relevant.
- **Concurrency or recovery:** include a deterministic race, restart, crash, or
  contention test rather than relying only on a single-process happy path.
- **Storage implementation:** exercise every affected backend. Do not infer
  Redis or Postgres correctness from an in-memory fake alone.
- **Framework integration:** test both synchronous and asynchronous paths when
  both are supported.
- **Reliability claim:** identify the source path and test that provide the
  evidence. Update the architecture or threat-model documentation if the
  promise, assumption, unsupported boundary, or residual risk changes.
- **Documentation:** verify commands, links, configuration keys, and examples
  against the current implementation.

The relevant evidence references are:

- [Failure and threat model](sdk/docs/FAILURE_AND_THREAT_MODEL.md)
- [Failure-mode catalog](sdk/docs/FAILURE_MODE_CATALOG.md)
- [Configuration reference](sdk/docs/CONFIG_REFERENCE.md)

### Real Redis and Postgres tests

CI runs the full test suite against Redis 7 and Postgres 16. Tests that require
those services may skip locally when their environment variables are absent.
A skipped backend test is not evidence that the backend works.

To exercise the same gates locally, start disposable Redis and Postgres
instances, then set:

```bash
export MYCELIUM_TEST_REDIS_URL="redis://127.0.0.1:6379/15"
export MYCELIUM_TEST_POSTGRES_DSN="postgresql://mycelium:mycelium@127.0.0.1:5432/mycelium_test"
export MYCELIUM_CI_REQUIRE_REDIS="1"
export MYCELIUM_CI_REQUIRE_POSTGRES="1"

cd sdk
uv run pytest tests/ -v
```

The `MYCELIUM_CI_REQUIRE_*` flags turn a missing backend into a failure instead
of a silent skip. Never point tests at production databases or shared customer
infrastructure.

## Reliability review standard

When a change touches execution, retry, reconciliation, identity, or durable
state, explain the complete state transition in the pull request:

1. What state exists before the operation?
2. What durable write or compare-and-set authorizes the operation?
3. What happens if the process crashes before, during, or after the external
   effect?
4. What can a concurrent or stale worker observe and write?
5. What evidence permits a retry?
6. Which cases fail closed?

Do not describe a behavior as “exactly once” unless the implementation and
external provider can actually prove that stronger property. Mycelium's core
contract is narrower: a stable effect identity gets one initial winning attempt
plus one attempt for each consumed, proven `NOT_EXECUTED` verdict. `UNKNOWN`
does not authorize another consequential effect.

Tests, Doctor output, Verify output, and provider conformance reports are scoped
evidence. Do not broaden what they prove beyond the backend, fault model, and
boundaries they exercised.

## Pull request guidelines

A strong pull request:

- Links the issue or clearly explains the concrete problem
- States the user-visible impact
- Includes a minimal reproduction for a bug
- Explains why the proposed behavior is correct
- Lists the exact tests run and whether any tests skipped
- Calls out compatibility, migration, storage, security, and concurrency impact
- Updates documentation when shipped behavior changes
- Contains no unrelated formatting, generated files, or dependency churn
- Is small enough to review without reconstructing several independent changes

Maintainers may ask for a smaller patch, stronger evidence, a design issue, or
real-backend testing before reviewing implementation details.

### Suggested pull request checklist

Copy the applicable items into the pull request description:

```markdown
## What changed

<!-- Describe the problem and the smallest solution. -->

## Evidence

<!-- Link the issue/reproduction and identify the source + tests that support the behavior. -->

## Validation

- [ ] Focused regression tests pass
- [ ] Full `pytest tests/ -v` passes
- [ ] `ruff check mycelium tests` passes
- [ ] Redis/Postgres tests ran, or the PR explains why they do not apply
- [ ] No relevant tests were silently skipped
- [ ] Compatibility and durable-state impact were reviewed
- [ ] Documentation and `CHANGELOG.md` were updated when required
- [ ] No secrets, private data, or production credentials are included
- [ ] AI or automated tooling is disclosed, and I reviewed every generated change
```

## AI-assisted and automated contributions

AI-assisted and automated contributions are allowed. They are reviewed by the
same standard as manually written work.

The human submitter is accountable for the pull request and must:

- Disclose meaningful use of AI agents, code generators, or automated patching
  in the pull request description
- Review and understand every submitted change
- Be able to explain the implementation, tests, and tradeoffs
- Verify all factual claims, links, APIs, and command output
- Reproduce the bug or user need independently of the generated explanation
- Run the relevant tests and report skips honestly
- Confirm that generated material does not copy incompatible code or text
- Respond to review feedback and maintain the contribution like any other
  author

Automation must not fabricate failures, test results, citations, users, security
impact, or compatibility claims. Passing CI alone is not proof that a change is
correct.

Unsolicited bulk PRs, duplicate automated fixes, unexplained generated rewrites,
and patches the submitter cannot support may be closed without detailed review.
Do not use automation to overwhelm issue or review queues.

A concise disclosure is enough:

> AI assistance: I used [tool] to help inspect or draft this change. I reviewed
> every submitted line, reproduced the problem, and ran the tests listed above.

Using AI is not a reason to reject a contribution. Hiding its material use or
submitting unverified output is.

## Compatibility and public API changes

Treat exported symbols, constructor signatures, serialized fields,
configuration keys, CLI output consumed by scripts, and durable storage formats
as compatibility-sensitive.

- Prefer additive, backward-compatible changes.
- Deserialize records written by supported older releases when practical.
- Preserve existing positional arguments when extending public callables or
  dataclasses.
- Include migration or rollback guidance for durable format changes.
- Update `sdk/mycelium/__init__.py` and API tests when adding public symbols.
- Discuss intentional breaking changes in an issue before implementation.

## Documentation and changelog

Keep documentation precise about guarantees and limitations. Examples must use
real public APIs and safe synthetic data.

For user-visible behavior changes, add a concise entry under `## Unreleased` in
`CHANGELOG.md` unless a maintainer asks to batch it differently. Documentation-
only or internal maintenance changes normally do not need a package release.

Do not bump `sdk/pyproject.toml` in an ordinary feature, fix, or documentation
pull request. Releases are deliberately batched. Version bumps and promotion of
`## Unreleased` belong in a dedicated release pull request following
[the release policy](sdk/docs/RELEASE.md).

Do not hardcode the current package version in README banners or badges.

## Commit and branch hygiene

- Branch from current `main`.
- Use descriptive commit messages that explain the intent of the change.
- Keep generated artifacts and lockfile changes limited to cases that require
  them.
- Rebase or merge current `main` when needed to resolve conflicts cleanly.
- Do not rewrite unrelated history or include local configuration, editor state,
  caches, build output, or credentials.

Maintainers may squash commits when merging.

## Review and conduct

Be direct, respectful, and evidence-led. Critique code and claims, not people.
Assume good intent, answer questions clearly, and update the pull request when
the evidence changes.

Opening a pull request does not guarantee that it will be merged. Maintainers
may decline work that is out of scope, duplicates another effort, weakens a
safety property, lacks reproducible evidence, or creates maintenance cost that
outweighs its benefit.

Contributions accepted into this repository are distributed under the
[MIT License](LICENSE). Submit only work you have the right to contribute.

## Support the project

If you find Mycelium useful, please consider giving the
[repository a GitHub star](https://github.com/mycelium-labs/mycelium). It helps
others discover the project—no pressure, and it is never a condition of issue
triage, review, or merge.

### Markdown link checks

Before opening a documentation-related pull request, run the same local Markdown
link and heading-anchor check used by CI:

```bash
lychee --verbose --no-progress --offline \
  --exclude-path graphify-out \
  --exclude-path .cache \
  "**/*.md"