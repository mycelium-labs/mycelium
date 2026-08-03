# Mycelium

[![PyPI version](https://img.shields.io/pypi/v/mycelium-runtime.svg?cacheSeconds=60&release=1.24.0)](https://pypi.org/project/mycelium-runtime/)
[![Python](https://img.shields.io/pypi/pyversions/mycelium-runtime.svg)](https://pypi.org/project/mycelium-runtime/)
[![Downloads](https://static.pepy.tech/badge/mycelium-runtime)](https://pepy.tech/project/mycelium-runtime)

**Runtime guards and zero-touch YAML auto-instrumentation for AI agents.**

Stops duplicate side effects on retry/redispatch, blocks bad tool args and out-of-scope calls, and keeps tool data fresh. Not recovery after. Not tracing or dashboards.

*Early but API-stable (**v1.24.0**): breaking changes only at major versions. More guards planned.*

## Who it's for

Developers running **agents with side-effect tools** in production (payments, emails, API writes, long subagent calls) on **LangGraph, CrewAI, or a plain Python loop**.

Python 3.10+. Framework-agnostic.

## What it does (v1.23.x)

These aren't reasoning failures. They're runtime failures. Mycelium sits between your agent loop and your tools (after the LLM returns `tool_calls`):

**Core** (`mycelium init` / `mycelium run`):

- **Duplicate side effects on retry:** classify tools (`read` vs `keyed_mutate` vs `non_idempotent_mutate`, etc.), hash a durable **transition key**, resolve duplicates by **terminal state** — not blind re-execute. **Do not redispatch unless the previous transition is proven terminal or safely recoverable.** This is a **transition envelope** (class + lease + terminal + hard-block / reconcile), not only an idempotency key plus a cached result.
  - **Read tools:** poll in-flight, reclaim expired leases, **soft-block** ambiguous `UNKNOWN` (safe retry by default)
  - **Mutating tools:** hard-block ambiguity; **reconcile** via `external_operation_ref` when a provider lookup can prove run-or-not (`COMPLETED` / `NOT_EXECUTED` / still blocked)
  - **Operator release (v1.15.0):** when a hard-block needs a human, an operator verifies with the provider and records it (`release(verified=...)` / `mycelium transitions release`) — `completed` returns the recorded result, `not_executed` grants exactly one re-execution. One-shot, fail-closed, audit-stamped; triage via `mycelium transitions list --stuck`
  - **Worker-death signal (v1.16.0, opt-in):** when `reclaim_requires_death_signal: true`, EXPIRED entries cannot be reclaimed or released without affirmative death evidence (`mark_worker_dead()` / `mycelium transitions mark-dead`, or heartbeat older than the grace window). Prevents reclaiming from a worker that is merely paused.
  - **Provider idempotency-key validity (v1.17.0):** when `provider_idempotency_key_ttl` is set, a same-key retry that exceeds the window hard-blocks instead of retrying — the provider may have purged its deduplication state.
  - **Atomicity contract (v1.18.0):** every terminal-outcome write uses CAS (`try_transition`) — already-resolved transitions refuse overwrites. Owner fencing in `@ledger`/`@ledger_sync` prevents stale workers from overwriting another worker's outcome.
  - **Gmail sent-log reconciler (v1.19.0):** email send tools fail after the provider accepts a message but before the 250 OK arrives — the ambiguous transition hard-blocks. `GmailReconciler` resolves it automatically by checking the Gmail sent-log (`in:sent rfc822msgid:<Message-ID>`); zero matches stays `UNKNOWN` (indexing lag), never a blind retry.
  - **Unclassified tools:** tools without a `transition_binding` have unknown side-effect semantics. `unclassified_policy: strict` routes retries through a conservative binding so failed retries hard-block instead of re-executing (default `warn` emits a one-time `UserWarning`). Side-effecting tools using memory storage get a one-time warning — the duplicate-side-effect guard only holds within the process.
  - **Stale lease (`EXPIRED`):** strict classes reclaim only when reconcile proves `NOT_EXECUTED` (fail-closed without a ref)
  - **Lease auto-renew (v1.14.0):** while a `@ledger` / `@ledger_sync` tool runs, Mycelium extends `lease_until` automatically (default every `lease_ttl / 3`) so long work does not look dead to a redispatched peer. Set `lease_renew_interval: 0` to disable; call `renew_lease()` for a manual bump or when claiming outside the decorator.
  - **LangGraph Cloud:** long tools may be redispatched around **~180s** (`BG_JOB_HEARTBEAT` sweep); Mycelium’s lease/poll/hard-block (with auto-renew) guards that window ([langgraph#7417](https://github.com/langchain-ai/langgraph/issues/7417))
- **Transition envelope fields** (priority order): `side_effect_class` → `spendability` → `side_effect_boundary` → `terminal_outcome` → `external_operation_ref` → `retry_permission` — payment/write needs the heavier set; without it, redispatch is an unsupported second transition, not a retry

**Opt-in** (configure or call explicitly):

- **Infinite action loops (AF-003):** `loop_guard:` detects the same tool + args across *new* `tool_call_id`s (the ledger only dedupes redispatches of the *same* id). Soft-blocks with `ToolBoundaryError` (`violation=loop_detected`), then hard-blocks the whole run (`LedgerHardBlockError`) until an operator runs `mycelium loops release --verified clear|allow-once|abort-run`. On in `mycelium init --full` / `--minimal`. Details: [sdk/README.md](sdk/README.md#loop-guard-af-003-identical-actions-across-new-tool_call_ids).
- **Premature termination (AF-007):** optional `completion:` host checklist — unmarked **required** subtasks **refuse** terminal (`CompletionRefusedError`); unmarked **optional** only warn. Mark `success` / `failed` / `abandoned`; gate via `complete_run()`, LangGraph END, or final-message wrap. Details: [sdk/README.md](sdk/README.md#completion-contract-af-007-refuse-terminal-while-required-subtasks-pending).
- **Superseded state / state authority:** optional `state_authority:` freezes a `state_ref` at decide time and compares it to a host canonical callback before claim — blocks stale-checkpoint redispatches that mint a *new* `tool_call_id` (claim ≠ state authority). Details: [sdk/README.md](sdk/README.md#state-authority-refuse-decisions-from-superseded-checkpoints).
- **Stale or broken context:** TTL-fresh tool data (`@protect`); optional message/history validation before the next LLM turn
- **Bad tool calls:** block invalid inputs and out-of-scope tools before they run (`@bounded` / registry)
- **Resolution telemetry + DTTR (v1.20.0):** opt-in `OutcomeEmitter` writes flat, append-only rows on resolution events; the **Duplicate Tool Transition Rate** makes the no-double-execute guarantee observable in production. Off by default; memory/file storage only (no analytics dependency); emission failures are logged and swallowed so telemetry never breaks the tool path.

Not Langfuse. Use both if you want traces and guards. Full resolution rules: [sdk/README.md](sdk/README.md#resolution-gates). Envelope field stack: [sdk/README.md](sdk/README.md#transition-envelope-fields). Payment-class identity guidance: [sdk/README.md](sdk/README.md#payment-class-identity-server-authoritative). Failure & threat model: [sdk/docs/FAILURE_AND_THREAT_MODEL.md](sdk/docs/FAILURE_AND_THREAT_MODEL.md). Inbound webhook event ids: [sdk/README.md](sdk/README.md#webhook-event-dedupe-optional).

Not an approvals/policy inbox, not hosted observability, not on-chain audit trails, not a generic webhook hub, and not a rewind/agent-memory tool. It stops unsafe re-execution of side effects at the tool boundary — prevention, not post-hoc healing. Approvals, traces, and chain anchors are adjacent layers it composes with, not features it competes on: [What Mycelium does not do](sdk/README.md#what-mycelium-does-not-do).

## Use it

```bash
pip install mycelium-runtime
pip install 'mycelium-runtime[langgraph]'  # automatic LangGraph runtime IDs
pip install 'mycelium-runtime[redis]'      # multi-worker / cloud ledger
pip install 'mycelium-runtime[postgres]'   # Postgres ledger backend
mycelium demo --slow       # feature tour, paced for screen recording
mycelium demo              # same tour, fast
mycelium demo --redis      # optional Cloud-style two-worker Redis proof (#7417)
mycelium init              # on-ramp: transition + one ledgered tool → mycelium.yaml
mycelium init --full       # reference: all guards (fill TODOs; not the default)
mycelium init --minimal    # smaller multi-guard scaffold
```

`mycelium demo --redis` runs two OS processes against a real Redis ledger — Worker B redispatches while A is in-flight; B polls and returns A's result. Needs Redis (`MYCELIUM_TEST_REDIS_URL` or `redis://127.0.0.1:6379/15`) and `pip install 'mycelium-runtime[redis]'`.

`mycelium init` is the real start path (duplicate-tool fix). Use `--full` when you want every section documented in one file.

```yaml
# after: mycelium init
integrations:
  langgraph:
    enabled: true

transition:
  agent_id: my-agent
  policy_version: "2026.07.1"
  # lease_ttl: 3600
  # lease_renew_interval: 1200   # default = lease_ttl/3; 0 disables auto-renew

action_ledger:
  storage: file
  path: ./mycelium-ledger.json
  unclassified_policy: strict   # warn (default) or strict
  tools: [my_side_effect_tool]

tools:
  my_side_effect_tool:
    callable: my_app.tools:my_side_effect_tool
    side_effect_class: non_idempotent_mutate
```

Launch your existing Python application without adding decorators:

```bash
mycelium run --config mycelium.yaml -- python -m my_app
```

`mycelium run` validates and wraps every configured callable before the
application starts. It preserves the child process's arguments, working
directory, signals, and exit code. The command accepts the current Python
interpreter only.

Explicit instrumentation remains supported when you prefer code-level control:

```python
from mycelium import load_config

config = load_config("mycelium.yaml")

@config.apply
def my_side_effect_tool(...) -> dict:
    ...
```

Without YAML, prefer the ledger decorators (`@ledger` / `@ledger_sync` for tools;
`@task_ledger` / `@task_ledger_sync` for coarser task-level idempotency). Same transition
envelope and gates — see [sdk/README.md](sdk/README.md#what-ledger--ledger_sync-do)
and [task-level idempotency](sdk/README.md#quickstart-task-level-idempotency).
If you own the tool runner and need explicit claim → execute → complete
(PROCEED/SKIP-style), see
[Manual integration](sdk/README.md#manual-integration-claim--execute--complete)
— same ledger; no YAML switch.

Do not combine standalone guard decorators with command mode on the same
function. Fully configured `@config.apply` wrappers are detected and skipped.
Keep callable modules import-safe: registrations performed inside a target
module while that module is still importing cannot be retroactively replaced.

With the optional LangGraph integration, `ToolNode` / `create_agent` injects
`ToolRuntime`; Mycelium automatically maps its `tool_call_id`, thread, run, and
node into transition identity. Explicit IDs still override captured values.
Custom tool executors can continue passing `tool_call_id` manually. Redispatch
resolves the existing transition: read tools poll/soft-block; mutating tools
hard-block or reconcile against the provider when you record
`external_operation_ref`.

Zero-ops single-node durable ledger: YAML `storage: sqlite` + `path:` (stdlib;
no extra install). Multi-worker / cloud: `pip install 'mycelium-runtime[redis]'`
or `'mycelium-runtime[postgres]'`. See the
[handbook](https://mycelium-labs.github.io/mycelium/).

## Docs

- **Handbook:** https://mycelium-labs.github.io/mycelium/
- **Full API reference:** [sdk/README.md](sdk/README.md)
- **PyPI:** https://pypi.org/project/mycelium-runtime/

## Release process

### One-time setup

Create a GitHub Personal Access Token with `contents: write` scope on this repo and add it as a repository secret named `RELEASE_PAT` at **Settings → Secrets and variables → Actions**. This is required because the tag push uses the PAT (instead of `GITHUB_TOKEN`) so that `publish.yml`'s `on: push: tags: v*` trigger fires — `GITHUB_TOKEN`-pushed tags cannot trigger other workflows.

### Per-release steps

1. Create a feature branch, make changes, open a PR to `main`.
2. CI (pytest + ruff on Python 3.10–3.13) must pass.
3. To release, bump the version in `sdk/pyproject.toml` and add a `## X.Y.Z (date)` section to `CHANGELOG.md` in the **same PR**.
4. Merge the PR. On push to `main`, automation:
   - Reads the version from `sdk/pyproject.toml`.
   - Checks whether tag `v{version}` already exists — if it does, exits quietly (doc-only or non-version merges release nothing).
   - Runs the SDK tests and ruff (Python 3.12) as a safety gate before tagging.
   - Creates an annotated tag `v{version}` and pushes it (via PAT so the tag-push triggers the publish workflow).
   - Creates a GitHub Release with notes extracted from the matching `CHANGELOG.md` section (falls back to auto-generated notes if extraction finds nothing).
   - The tag push triggers [publish.yml](.github/workflows/publish.yml) (`on: push: tags: v*`) which builds and uploads to PyPI via trusted publishing.

**Manual escape hatch:** pushing a `v*` tag or triggering `workflow_dispatch` on the publish workflow still works — the existing manual path is unchanged. If the automation fails, publish manually by running `git push origin v{version}` locally after merging.

## License

MIT. See [LICENSE](LICENSE).
