# Failure-mode catalog (AF-001…AF-012)

**The taxonomy is the product story.** Mycelium is the reliability layer for AI
agents; these IDs make tool actions reliable across their full lifecycle:
validation and authority before execution, runtime control during the run, and
outcome resolution and evidence afterward. No single AF class defines the
whole product. Stable across the SDK README, handbook, and changelogs. Sourced
from a GitHub-issue corpus across LangChain, LangGraph, CrewAI, AutoGen, Cline,
OpenHands, and related stacks.

| ID | Failure mode | Mycelium surface |
|----|--------------|------------------|
| AF-001 | Hallucination cascade | (judgment / evals; not in SDK) |
| AF-002 | Observability black hole | Any tool / any provider: prove run-or-not · at-most-once (ledgers · reconcile · receipts; adapters are demos) |
| AF-003 | Infinite reasoning loops | `loop_guard:` / `@loop_guard` |
| AF-004 | Tool misuse | `@bounded` · `ToolRegistry` · `ToolRunner` |
| AF-005 | Goal misalignment | (judgment / evals; not in SDK) |
| AF-006 | Context corruption | `@protect` · `Session` · `MessageValidator` · `HistoryGuard` |
| AF-007 | Premature termination | `completion:` / `complete_run` |
| AF-008 | Cascading permission | `scope_guard:` / `@scope_guard` |
| AF-009 | Instruction injection | (MCP gateway revisit; not in SDK) |
| AF-010 | Secret-in-args | `secret_args:` / `SecretInArgsError` · `secret://` references · shared sanitizer |
| AF-011 | Destructive confirm | `destructive_confirm:` / `DestructiveGrantError` · host-issued object grant · fail closed before claim |
| AF-012 | Use-time currency | `use_time_currency:` / `UseTimeCurrencyError` · revalidate decide-time facts at use · fail closed before side effect |
| — | Exfil via write / destination policy | `entity_guard:` / `EntityGuardError` · host allowlist · fail closed before claim |

---

## AF-001 Hallucination cascade

**Class:** Decision validity / judgment

**One line:** Agent confidently acts on fabricated facts; errors compound across tool calls.

The agent invents or misstates facts, then treats those fabrications as ground
truth for later tool calls. Wrong lookup → wrong write → wrong confirmation.

**What users hit:** unverified tool returns treated as truth; fabricated IDs /
amounts / paths into mutating tools; plans that never re-ground against source
context.

**Why not a deterministic SDK guard yet:** needs a judge model
(precision/recall tradeoffs). Outside the deterministic `ALLOW` / `BLOCK`
chassis.

---

## AF-002 Observability black hole

**Class:** Execution / audit

**One line:** Consequential actions leave no durable, trustworthy record:
retries double-run; crashes leave unknown commit.

This names the *failure class*, not a tracing product. Mycelium ships
**prevention guards** (ledgers, leases, gates, optional signed receipts), not
spans/dashboards.

**What users hit:** framework redispatch doubles a payment/email/write; crash
mid-action with unknown commit; logs that cannot stand as auditor-verifiable
proof.

**AF-002 promise:** any tool, any provider — prove run-or-not and enforce
at-most-once. This is the catalog's execution/recovery capability, not the
product identity. Gmail is the only adapter currently shipped; other providers
use the same `Reconciler` contract and must supply their own adapter.

**Guards:** transition envelope · `ActionLedger` / `TaskLedger` · reconcile ·
`StateFlush` · audit receipts · default-on `on_args_drift` (identity-conflict;
default `soft`). `mycelium doctor` inspects configuration and detectable
wiring. `mycelium verify` empirically exercises synthetic failure scenarios
against the configured backend (never application tools, LLMs, or real
providers). Passing Verify does not prove a real business provider is correct.
Run `mycelium providers verify <adapter>` before shipping a provider adapter.
The conformance kit rejects false `NOT_EXECUTED`, ambiguous evidence treated as
safe, duplicate matches, malformed handles, and observed writes, then emits a
signed report bound to the tested adapter source. The report is synthetic
evidence: it does not prove the live credential is read-only or the provider is
available.
See the [SDK README](../README.md) and
[failure & threat model](FAILURE_AND_THREAT_MODEL.md).

---

## AF-003 Infinite reasoning loops

**Class:** Run-level reliability

**One line:** Same action pattern across distinct dispatches; no progress,
burns tokens / duplicates effects.

The ledger dedupes redispatches of the *same* `tool_call_id`. AF-003 is when
the model mints a **new** id each turn with the same tool + args (new
transition key every time).

**What users hit:** tight tool+args loops; “try again” with no strategy change;
token burn; repeated real side effects for mutating tools.

**Guard:** `loop_guard:` action-hash streak → soft `ToolBoundaryError` then
hard `LedgerHardBlockError`; operator `mycelium loops release`.

---

## AF-004 Tool misuse

**Class:** Safety / tool boundary

**One line:** Tool calls with invalid inputs or outside intended scope; silent
failure or wrong side effects.

**What users hit:** missing/wrong-typed args; path or entity outside allowed
scope; bad return shapes; tools not on the allowlist; no recovery path for the
LLM.

**Guards:** `@bounded` / `bounded_sync` (input/output/entity/path) ·
`ToolRegistry` · `ToolRunner`.

**Filesystem boundary:** `allowed_paths` resolves existing symlinks and fails
closed on resolution errors before dispatch. It is not a sandbox and cannot
prevent a concurrent writer from replacing path components after validation.
Use OS isolation or descriptor-relative file operations for hostile shared
filesystems.

---

## AF-005 Goal misalignment

**Class:** Decision validity / judgment

**One line:** Optimizes a proxy objective, not user intent.

The agent closes the ticket / finishes a checklist / maximizes tool success
while missing the user's actual goal. Wrong success still looks “done.”

**What users hit:** proxy metrics over declared intent; objective drift;
“success” against the wrong condition.

**Why not core yet:** judge-model / evals territory (same product-tier caveat
as AF-001). AF-007 only gates an *explicit* host checklist, not open-ended goals.

---

## AF-006 Context corruption

**Class:** Context

**One line:** Stale, truncated, or poisoned context → false picture of the world.

**What users hit:** stale tool/environment state; broken transcripts (orphan
tool results, duplicate ids, bad roles); oversized history silently truncated;
cross-request cache bleed.

**Guards:** `@protect` / `Session` · `MessageValidator` · `HistoryGuard`.

---

## AF-007 Premature termination

**Class:** Run-level reliability

**One line:** Stops early and presents partial work as complete.

**What users hit:** early stop with incomplete checklists; missing required
side effects before “success”; declared subtasks never resolved.

**Guard:** `completion:` host `required` / `optional` checklist; unmarked
required → refuse (`CompletionRefusedError`); unmarked optional → warn and
allow. Supported frameworks wire LangGraph END automatically when
`integrations.langgraph.enabled` is set. Production verifies an
explicitly selected adapter at startup. Manual fallback:
`complete_run()`, `gate_graph_end`, `wrap_final_message`.

---

## AF-008 Cascading permission

**Class:** Safety / scope

**One line:** Narrow grant escalates transitively beyond intent.

**What users hit:** allowlist drift across a long run; handoff / subagent
inherits broader tools than the parent grant.

**Guard:** `scope_guard:` freeze the run tool allowlist (from `registry` /
`tools:`) and re-check every step. Mid-run widen →
`ToolBoundaryError` (`scope_escalation_tool`). Entity/path/output stay on
`@bounded` (AF-004).

---

## AF-009 Instruction injection

**Class:** Safety / content

**One line:** Untrusted content hijacks instructions.

External data (tool output, retrieved docs, pasted text) contains instructions
that override the system/developer prompt. The agent follows the injected
policy.

**What users hit:** poisoned tool returns or RAG chunks; “ignore previous
instructions” payloads; instruction/data boundary collapse in context.

**Why not core yet:** revisit at the **MCP gateway**, where taint isolation is
mechanically enforceable. Complementary to dedicated prompt-injection products.

---

## AF-010 Secret-in-args

**Class:** Credential / evidence leakage

**One line:** Raw credentials, tokens, passwords, and private keys must not
reach tool arguments, receipts, ledgers, logs, exceptions, telemetry,
outcomes, fingerprints, or verification artifacts.

**What users hit:** an LLM or caller pastes an API key into a tool call;
the value is fingerprinted, claimed, receipted, logged, and left in
Doctor/Verify dumps.

**Guard:** optional `secret_args:` (`error` | `redact` | `warn`). Fail-closed
pre-execution blocking is the primary protection. Redaction of persisted
and emitted representations is defense-in-depth. Pass `secret://…`
references instead of credentials; resolve only at the trusted
tool/provider call for fields the tool declared. Applications must
`register_secret_resolver` — Mycelium does not invent a default
environment-variable resolver. `allow_fields` / `allow_tools` weaken
protection and must be scoped narrowly by tool, not globally trusted.
Mycelium cannot sanitize logs created inside arbitrary application or
provider code; Doctor labels those paths `not_verifiable`.

---

## AF-011 Destructive confirm

**Class:** Unauthorized irreversible mutation

**One line:** Tool permission is not object authorization. A delete, refund,
cancel, settle, revoke, terminate, purge, or overwrite executes only when
the host has granted that exact operation on that exact canonical object.

**What users hit:** a model with `refund_payment` permission refunds the
wrong payment, or retries a refund after an ambiguous crash because a
second grant existed.

**Guard:** optional `destructive_confirm:`. Listed tools declare the
canonical operation, object type, and argument path for the object id.
The host mints a `DestructiveGrant` through `issue_destructive_grant` /
`destructive_grants.issue` and places it on `TransitionScope`. Enforcement
runs after ordinary argument validation and before ledger claim, lease,
tool body, or any side effect. Missing, expired, exhausted, mismatched,
or unverifiable grants raise `DestructiveGrantError` and do not claim.
Retries with the same stable `request_id` reuse the ledger result and do
not consume a second use. Dual control is intentionally not implemented
— two-person approval belongs in the host workflow that issues the grant.
A grant authorizes an attempt; it does not prove the provider outcome.
Omitted `destructive_confirm:` keeps existing behavior.

---

## Authority-window expiry (unnumbered; batch with AF-011)

**Class:** Authorization timing / side-effect boundary

**One line:** No consequential operation may cross its side-effect boundary
using authority that expired after authorization but before use.

**What users hit:** a host grant was valid when the tool was claimed, but
expired while waiting for a lease, queue, backoff, or confirmation — and
the body still ran.

**Guard:** `authority_window:` plus use-time checks on registered
`BoundAuthority` (including AF-011 destructive grants). Validate at
authorize and again immediately before `mark_maybe_crossed` / provider /
body. `AuthorityExpiredError` hard-blocks; no auto-renew. Completed
ledger RETURN does not require fresh authority. Pairs with AF-012
use-time currency for fact freshness beyond expiry.

---

## AF-012 Use-time currency

**Class:** Authorization freshness / side-effect boundary

**One line:** A decide-time fact that is stale, changed, missing,
unverifiable, or outside its freshness window at execute cannot authorize
a consequential side effect.

**What users hit:** an account was refundable when the agent planned the
refund, but ownership, inventory, policy, or refundability changed while
waiting for a lease — and the body still ran on decide-time truth.

**Guard:** `use_time_currency:` / `use_time_facts.capture` /
`register_use_time_validator`. Authorize binds host facts; use revalidates
immediately before the side-effect boundary (after authority-window
expiry). `UseTimeCurrencyError` hard-blocks. `age >= max_age_seconds` is
stale. No prompt scanning. Completed RETURN does not revalidate. Does not
eliminate the remote-call race; provider preconditions narrow it when
declared.

---

## Budget enforcement (unnumbered)

**Class:** Run-level reliability / blast-radius

**One line:** Host-declared cost / token / wall-clock / step ceilings refuse
the next LLM or tool step when crossed.

**What users hit:** varied-tool overnight burn; pure LLM chat loops with no
tool boundary; stuck planners with no wall-clock ceiling.

**Guard:** `budget:` / `@budget_guard` — `max_duration`, `max_steps`,
`max_tokens`, `max_usd` (any subset). `@budget_guard` on tools; wrap the model
once automatically on LangGraph/LangChain (or `instrument_llm` / `@budget_llm`
/ manual `check("llm")` for custom providers) so LLM turns cannot skip the
gate; atomic `record_usage` for tokens/USD; `missing_usage_policy`; soft warn
then `LedgerHardBlockError`
on the next step (never mid-flight kill). Operator
`mycelium budget status|release`. Loop guard (AF-003) ≠ spend budget.
Budget is intentionally **not** given an AF-00N id. See
`notes/catalog/budget-enforcement.md`.

---

## How to read the IDs

- **AF-00N** = failure *class* from the corpus taxonomy, not a PyPI version.
- Feature docs use the ID in headings (e.g. “Loop guard (AF-003)”) so you can
  jump from a changelog line to the definition here.
- Ledger-core guarantees (AF-002 prevention) are spelled out in
  [FAILURE_AND_THREAT_MODEL.md](FAILURE_AND_THREAT_MODEL.md); other AF modules
  are optional guards documented in the SDK README.
- **Budget enforcement** ships as `budget:` / `@budget_guard` and is
  intentionally **not** given an AF-00N id. AF-010 is secret-in-args.
- **Destination policy** ships as `entity_guard:` and is also unnumbered:
  a write may carry sensitive data only into a host-authorized destination.
- **AF-011** is destructive confirm: tool permission is not object
  authorization.
