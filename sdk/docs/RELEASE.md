# Release policy & pre-release checklist

Mycelium is a **reliability layer**. Buyers (platform / infra engineers) trust
**calmness**, not velocity. A flood of PyPI versions reads as unstable — the
opposite of the product story.

**Batch. Prefer fewer, coherent releases over many small ones.**

---

## Why this policy exists (honest)

Through mid-2026 Mycelium cut a high volume of package versions while landing
the AF-00N catalog (on the order of **~26 MINOR bumps in ~3.5 months**). That
pace answered “are you building?” and failed the trust test for a reliability
layer: a thrashy changelog looks unstable.

**Fixing that is operational discipline, not a new SDK feature.** We keep
shipping code to `main`; we stop treating every merge as a PyPI event.
Semantic versioning applies only when a cut has **real user-facing change**
(behavior, API, packaging). Docs-only and positioning wait for the next real
batch. Quiet weeks with no PyPI cut are healthy.

Company milestone ≠ release count. One public production user on payment /
consequential tools beats the whole version arithmetic.

---

## Cadence rules (must follow)

1. **Batch by default.** Merge work to `main` without bumping
   `sdk/pyproject.toml`. Accumulate fixes, docs, and features. Cut **one**
   version when a coherent batch is ready.
2. **No same-day PyPI spam.** Do not publish multiple `mycelium-runtime`
   versions on the same calendar day unless a **critical** correctness /
   security fix requires a hotfix (document why in the CHANGELOG).
3. **Target rhythm:** about **one release per week** (or slower) when there is
   real user-facing change. Quiet weeks with no PyPI cut are healthy.
4. **Docs / positioning alone** usually wait for the next real batch — do not
   cut a patch solely to refresh README copy unless PyPI metadata must change
   for outreach *and* nothing else is pending (still never same-day thrash).
5. **Version bump = intentional release.** The only signal that tags/publishes
   is a new version in `sdk/pyproject.toml` (plus matching `CHANGELOG.md`).
   Merging without a bump publishes nothing — use that.
6. **Semver on real change only.**
   - **PATCH** — bugfixes, proofs, packaging, docs that ship *with* a real
     fix batch (no new schema/policy concepts).
   - **MINOR** — new backward-compatible durable fields or resolution
     behavior (batch several related landings into one MINOR when possible).
   - **MAJOR** — breaking defaults or removed paths.
   Do **not** mint a MINOR per merged feature PR. That is how ~26 minors
   happens.
7. **Hotfix exception:** production-breaking ledger/reconcile bug or security
   issue → ship a focused PATCH immediately; still one cut, clear notes, no
   pile-on features in the same tag.

Cadence is orthogonal to semver — batch *what* goes into each bump.

---

## Version-line hygiene (docs churn)

**Only real SDK changes may move the version line.** Hand-editing README /
handbook / badge URLs to name `vX.Y.Z` on every merge is the same failure mode
as PyPI spam — it makes the product look unstable (#71).

Rules:

1. **`sdk/pyproject.toml` is the only version source of truth.** Do not bump it
   on docs-only, CI, or positioning PRs.
2. **Do not hardcode the current package version** in root/`sdk` README prose
   or shields.io badge query params (`&release=`). Badges track PyPI; prose
   links to PyPI. Historical "shipped in vX.Y.Z" mentions in CHANGELOG / docs
   stay — those are history, not a "current version" banner.
3. **Batch docs/site/handbook edits into the release PR** (or a docs PR that
   does **not** touch `pyproject.toml`). Never open a version-bump PR just to
   refresh copy.
4. **CHANGELOG:** land notes under `## Unreleased` on feature PRs; promote to
   `## X.Y.Z (date)` only in the release PR. Prefer summarizing the batch from
   git history over a diary of micro-commits.
5. **CI enforces** that a `pyproject.toml` version change requires a matching
   `CHANGELOG.md` `## X.Y.Z` header (see `.github/workflows/ci.yml`).

---

## Before you bump the version (checklist)

Complete **every** applicable item. If something does not apply, write N/A in
the PR description — do not skip silently.

### Intent

- [ ] This cut is a **batched** release (or a justified hotfix), not “ship
      because the PR is ready.”
- [ ] No other `mycelium-runtime` version was published **today** (unless hotfix).
- [ ] CHANGELOG tells a **coherent story** (why this batch, not a diary of
      micro-commits).
- [ ] Positioning describes full-lifecycle tool-action reliability (validation
      and authority before execution; runtime control; outcome resolution and
      evidence afterward). Public onboarding uses plain-language risk names,
      numbered incident IDs stay internal, Gmail is an example adapter, and
      release velocity is not presented as a virtue.

### Correctness

- [ ] `pytest tests/` passes locally from `sdk/` (full suite).
- [ ] `ruff check mycelium tests` clean from `sdk/`.
- [ ] CI green on the release PR (3.10–3.13), including the `reproducible-build`
      job (see [Reproducible builds](#reproducible-builds)).
- [ ] New guarantees mapped to tests (or explicitly “docs-only / no new
      guarantee” in CHANGELOG).
- [ ] Hotfix: repro + regression test included.

### Surface area

- [ ] Public symbols exported from `sdk/mycelium/__init__.py` if new API.
- [ ] YAML template / `mycelium init` updated if new config surface.
- [ ] SDK README (+ root README if user-facing) updated for shipped behavior.
- [ ] Failure-mode catalog / threat model updated if promise or residual risk
      changed.
- [ ] Handbook (`mycelium-labs.github.io`) version/lede not wildly stale for
      user-visible cuts (batch handbook updates with the release when practical).

### Language-neutral protocol and clients (when applicable)

- [ ] A wire-visible change uses a new protocol revision; frozen `v1alpha1`
      behavior was not silently changed.
- [ ] Python sidecar OpenAPI, Transition Envelope schema/fixtures, examples, and
      protocol status document agree.
- [ ] TypeScript client passes typecheck, build, and `npm pack --dry-run`; Go
      client passes `go vet ./...`, `go test ./...`, and `go build ./...`.
- [ ] Client versions, Git tags, npm distribution tags, Go module tags, README
      install commands, and CHANGELOG publication claims agree.
- [ ] Publishing npm or Go artifacts received separate explicit approval; a
      Python release does not implicitly publish them.

### Versioning artifacts (same PR)

- [ ] `sdk/pyproject.toml` `version` bumped once for this cut.
- [ ] `CHANGELOG.md` has `## X.Y.Z (YYYY-MM-DD)` matching that version
      (promote from `## Unreleased`; summarize the batch).
- [ ] Root + SDK README do **not** hardcode `vX.Y.Z` as "current" (badges /
      PyPI links only). Handbook site updated in its own repo if the cut is
      user-visible.
- [ ] No second bump planned the same day.

### After merge (automation)

On merge to `main`, [release.yml](../../.github/workflows/release.yml) tags
`v{version}` if the tag does not exist, then [publish.yml](../../.github/workflows/publish.yml)
uploads to PyPI. Confirm:

- [ ] GitHub Release notes look right (from CHANGELOG extract).
- [ ] GitHub Release includes the CycloneDX SBOM artifact (`mycelium-runtime-X.Y.Z.cdx.json`).
- [ ] PyPI shows the new version: https://pypi.org/project/mycelium-runtime/
- [ ] Hotfix: notify affected design partners; otherwise no “blast” needed.

---

## Software Bill of Materials (SBOM)

Releases automatically generate and attach a machine-readable Software Bill of Materials (SBOM) in CycloneDX JSON format (`mycelium-runtime-<version>.cdx.json`) as a release asset in GitHub Releases and as a workflow artifact in GitHub Actions.

### Reproducing and verifying locally

To generate and verify the CycloneDX SBOM locally:

1. Install build tools and `cyclonedx-bom`:
   ```bash
   pip install build "cyclonedx-bom>=4.0.0"
   ```

2. Build the package wheel and sdist:
   ```bash
   python -m build sdk
   ```

3. Install the built wheel into a clean virtual environment:
   ```bash
   python -m venv .sbom-env
   .sbom-env/bin/pip install sdk/dist/*.whl
   ```

4. Generate the CycloneDX SBOM:
   ```bash
   cyclonedx-py environment .sbom-env/bin/python \
     --pyproject sdk/pyproject.toml \
     --mc-type library \
     --output-format JSON \
     --output-file sdk/dist/mycelium-runtime.cdx.json
   ```

5. Verify the generated SBOM with standard tooling:
   ```bash
   python -c "import json; data = json.load(open('sdk/dist/mycelium-runtime.cdx.json')); print('Format:', data['bomFormat'], data['specVersion']); print('Root:', data['metadata']['component']['name'], data['metadata']['component']['version'])"
   ```

---

## Reproducible builds

CI builds the wheel and sdist **twice from the same commit** and fails if the two
builds differ. Users and downstream packagers can then trust that the files
published to PyPI contain exactly what the tagged commit says they contain.

The check ([`check-reproducible-build.py`](../../.github/scripts/check-reproducible-build.py))
runs each build in its own clean source export and its own fresh virtual
environment. Both install only the toolchain pinned in
[`reproducible-build-requirements.txt`](../../.github/reproducible-build-requirements.txt),
because `sdk/pyproject.toml` asks for an unpinned `hatchling`. It needs no
publishing rights or credentials, only read access to a package index.

Run it locally from the repository root (it builds the last commit, not
uncommitted changes):

```bash
python .github/scripts/check-reproducible-build.py
python .github/scripts/check-reproducible-build.py --commit HEAD~1 --keep-artifacts /tmp/repro
python .github/scripts/check-reproducible-build.py --require-identical-bytes
```

**What is compared.** For both the wheel and the sdist: every member's path,
type, content, executable bit, and symlink target, plus every metadata header
(`METADATA`, `WHEEL`, `PKG-INFO`).

**What is normalized (documented, expected).** These differences do not change
what pip installs, so they never fail the check. The report lists any that
actually occurred:

| Field | Why it is ignored |
|-------|-------------------|
| Member modification times (zip and tar) | Clock values, not content |
| Tar `uid`, `gid`, `uname`, `gname` | Who ran the build |
| Member order inside the archive | Same files, different listing order |
| Sdist gzip header (mtime, OS byte) | Compression framing |
| Zip compression level, extra fields, creator OS, permission bits other than the executable bit | Archive-writer framing |

`--require-identical-bytes` also fails when the archives differ byte-for-byte,
even if every member matches.

**What is deliberately varied** so the check can find real problems: the
absolute source directory, the temporary and virtual-environment locations, `TZ`,
and `PYTHONHASHSEED` (left random, so set/dict ordering bugs surface).
`SOURCE_DATE_EPOCH` is set to the commit time for both builds.

**When it fails.** The report names each member that differs, shows a diff, and
explains the usual cause: a timestamp or random id written into a file, an
absolute build path, an unordered collection in generated metadata, or a
build-machine-dependent file mode. The CI job uploads both builds
(`reproducible-build-diff`) so they can be compared with a tool such as
`diffoscope`.

**Refreshing the toolchain pins.** In a scratch virtual environment run
`pip install build hatchling` and `pip freeze`, copy the new versions into
`.github/reproducible-build-requirements.txt`, and run the check. Bumping is
safe: the check compares two builds made with the same pins, never against an
older release. A build requirement added to `[build-system]` without a pin makes
the check fail before it builds.

## How to cut a release (mechanics)

1. Land work on `main` via PRs **without** version bumps (preferred). Docs and
   site edits land the same way — no `pyproject.toml` touch.
2. Open a **release PR** that only (or primarily):
   - bumps `sdk/pyproject.toml`
   - promotes `## Unreleased` → `## X.Y.Z (date)` in CHANGELOG (batch summary)
   - optionally batches handbook / positioning that waited for the cut
3. Pass the checklist above in the PR body (copy the checkboxes).
4. Merge. Do not manually tag unless automation fails (see root README escape hatch).

**Do not** open a version-bump PR for every merged feature. That is how
8-releases-a-day happens. **Do not** bump README "current version" strings on
feature PRs — that is how 10 version lines show up in a week of docs churn.

---

## What “batch” means in practice

| Good | Bad |
|------|-----|
| One MINOR after loop + completion + scope docs polish | Three PATCHes the same afternoon for each guard |
| Weekly PATCH with five fixes + one docs pass | PATCH per fix “so partners see progress” |
| Quiet week, no PyPI cut | Feeling obligated to ship daily |
| Hotfix PATCH alone, then resume batching | Hotfix + three drive-by features |

Trust compounds when the version line moves **rarely and for good reason**.

**Company milestone ≠ release count.** One public production user on payment
tools is worth more than the whole roadmap. Ship calmly so that user can trust
the layer — do not confuse PyPI version arithmetic with product validation.
