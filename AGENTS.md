# AGENTS.md

Runtime guards for AI agents (PyPI: `mycelium-runtime`, import: `mycelium`). Python 3.10+, pydantic v2 + pyyaml only.

## Layout

- The package lives in `sdk/` — not the repo root. Run all Python dev from `sdk/`.
- `docs/` is a hand-written static handbook (`index.html`); pushing to `main` with `docs/**` changes auto-deploys it to GitHub Pages.
- `randm/`, `TODO.md`, `rules.md`, `READING_LIST.md`, `.cursor/` are gitignored local-only files. Never commit them or reference them from shipped code/docs.
- `.env` (see `.env.example`) is optional — HF corpus access only. SDK dev and tests need nothing from it.

## Commands

- Install: `pip install -e "./sdk[dev]"` (from repo root)
- Test: `pytest tests/` (from `sdk/`; full suite ~1–2s). Single: `pytest tests/test_transition.py::test_name`
- Lint: `ruff check mycelium tests` (from `sdk/`; line-length 100, rules E/F/I/UP, py310 target)
- CI is exactly those two steps on Python 3.10–3.13. No typechecker, pre-commit hooks, Makefile, or codegen.

## Testing quirks

- `asyncio_mode = "auto"` — async tests need no marker/decorator.
- Redis backend tests monkeypatch in `fakeredis` (dev dep); no real Redis needed.
- Postgres integration tests skip unless `psycopg` is installed AND `MYCELIUM_TEST_POSTGRES_DSN` is set.

## Architecture

- Core idea: a durable "transition" envelope around side-effecting tool calls so framework retries/redispatches can't double-execute. Flow: `transition.py` (binding: `side_effect_class`, `spendability`, `retry_permission`) → `action_ledger.py` (claim/complete; memory + file storage) → `transition_resolution.py` (poll / allow / hard-block) → `reconcile.py` (opt-in provider reconcile loop). Redis/Postgres storage in `storage/` behind the `redis` / `postgres` extras.
- Public API is flat: new public symbols must be exported from `sdk/mycelium/__init__.py` (changelog treats "export from package root" as a required release step).
- CLI (`mycelium init|demo`) entry is `mycelium/__main__.py`; YAML scaffolds in `mycelium/templates/`; `mycelium/proofs/` + `fixtures/` reproduce langgraph#7417 end-to-end.

## Versioning & release

- `sdk/pyproject.toml` is the only version source of truth. READMEs/docs may lag it (README said v1.7.0 while pyproject was 1.8.0) — don't "sync" them as part of code changes.
- Bump rules (from gitignored `rules.md`): PATCH = fixes/proofs/docs/packaging, no new schema/policy concepts; MINOR = new durable fields or resolution behavior, backward-compatible with existing YAML; MAJOR = breaking defaults or removed paths.
- `policy_version` in user YAML is unrelated to the package version.
- Release: bump `pyproject.toml` + `CHANGELOG.md`, push tag `v*` → `publish.yml` builds from `sdk/` and publishes to PyPI via trusted publishing.
- Commits use `feat:` / `docs:` / `chore:` prefixes; feature releases suffix the version, e.g. `feat: ... (v1.7.0)`.
- Never add AI-agent attribution (co-author trailers, made-with/generated-by footers) to commits or PRs — repo rule from `.cursor/rules/`.
