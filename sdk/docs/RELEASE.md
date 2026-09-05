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
- [ ] Positioning matches the reliability-layer story (catalog / AF-002
      flagship; Gmail = demo adapter; no velocity-as-virtue language).

### Correctness

- [ ] `pytest tests/` passes locally from `sdk/` (full suite).
- [ ] `ruff check mycelium tests` clean from `sdk/`.
- [ ] CI green on the release PR (3.10–3.13).
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
