# Durable composite recovery

Mycelium supports a bounded composite protocol for unchanged, sequential
Python functions whose consequential calls already pass through Mycelium's
`ledger`/`ledger_sync` wrappers. A non-effect helper may be explicitly marked
with `register_composite_helper`; an unresolved call is otherwise a setup
error, never an ignored manifest entry.

```python
from mycelium import composite

@composite(
    storage=storage,
    operation_id_from=lambda _args, kwargs: kwargs["job_id"],
)
def publish_change(job_id: str):
    commit = create_commit(idempotency_key=f"{job_id}:commit")
    pushed = push_branch(idempotency_key=f"{job_id}:push")
    return update_tracking_record(
        idempotency_key=f"{job_id}:tracking", pushed=pushed
    )
```

The child wrappers remain ordinary ledger transitions. The outer decorator
builds a manifest before execution, then stores it with a digest in a durable
composite-control record. That record owns the logical operation ID,
definition, lease, monotonically increasing parent fence, lifecycle, and child
bindings. The child identity extends the existing transition preimage with
the composite namespace, pinned definition, and a semantic call-site step ID
that is independent of the checkout or deployment path; it does not replace
scope, dispatch, tool, arguments, destination,
side-effect, agent, policy, or identity-schema fields.

On replay the unchanged function runs from the beginning. A completed child
returns its durably serialized and faithfully reconstructable result, while an unattempted child is admitted
under the current parent fence and executes through its existing ledger
protocol. Ambiguous children still reconcile or hard-block. Parent authority
is checked at admission and immediately before the child body and
`mark_maybe_crossed()` boundary. This protects progression but cannot cancel
an external request already sent during a check-to-send race.

The Python decorator supports straight-line assignments, expression
statements, and one final return, with each supported call at a statement
boundary. It also supports one top-level `if`/`else` whose condition is a
boolean function argument (`if enabled:` or `if not enabled:`). The argument
must be an actual `bool` and cannot be reassigned. Both possible paths are
checked at decoration time; the chosen path is pinned in the durable manifest
before the first child effect. A retry with a different choice is rejected.
Each path must contain at least one supported child boundary. For example:

```python
@composite(storage)
def publish_or_hold(operation_id: str, publish: bool):
    if publish:
        decision = publish_change(idempotency_key=f"{operation_id}:publish")
    else:
        decision = record_hold(idempotency_key=f"{operation_id}:hold")
    return record_decision(idempotency_key=f"{operation_id}:record", decision=decision)
```

The branch may instead depend on a `bool` returned by the immediately
preceding consequential child. Wrap that value with `composite_choice()` in
the `if` condition:

```python
from mycelium import composite, composite_choice

@composite(storage)
def publish_after_check(operation_id: str):
    allowed = check_release(idempotency_key=f"{operation_id}:check")
    if composite_choice(allowed):
        result = publish_change(idempotency_key=f"{operation_id}:publish")
    else:
        result = record_hold(idempotency_key=f"{operation_id}:hold")
    return result
```

The check is an ordinary ledgered child with a durable, faithfully replayed
boolean result. Both possible path definitions are pinned before the check
runs, including when the host supplies an explicit workflow version. Mycelium
stores the selected path under the parent fence
after that child resolves and before any branch child executes. A crash after
selection replays the check's stored result, verifies the same path, and
continues with stored child results. A changed choice blocks. The check must
be assigned immediately before the `if`; direct reads and mutable external
facts cannot determine a replayable branch.

One top-level fixed-count loop is also supported:

```python
@composite(storage)
def publish_batch(operation_id: str):
    for index in range(3):
        result = publish_one(idempotency_key=f"{operation_id}:{index}", index=index)
    return result
```

The `range(N)` count must be a literal integer from 1 through 32, and `range`
must be the built-in. Mycelium expands the loop into a pinned ordered manifest
before the first effect. Every iteration has a distinct child and request
identity, even when it calls the same tool with otherwise identical arguments.
Use a distinct provider idempotency key for each iteration when the provider
supports one; the ledger's child identity does not change provider key semantics.
On replay, completed iterations return stored results and execution continues
at the first unresolved iteration. Changing the loop shape or count blocks
recovery, including when an explicit `definition` is supplied. The loop
variable cannot be reassigned. This first loop slice cannot be combined with a
branch in the same composite.

A host may supply the bounded items at invocation time with the explicit
`composite_items()` iterator:

```python
from mycelium import composite, composite_items

@composite(storage)
def publish_batch(operation_id: str, items: list[dict]):
    for item in composite_items(items, max_items=32):
        result = publish_one(
            idempotency_key=f"{operation_id}:{item['id']}", item=item
        )
    return result
```

`items` must be a function argument containing a list of 1 to `max_items`
faithfully JSON-serializable values. `max_items` is a literal integer from 1
through 32. Before the first child effect, the decorator includes a digest of
the ordered item values and loop definition in the durable parent manifest;
the values themselves are not stored there. Replay requires the host to pass
the same items in the same order. `composite_items()` checks the list again at
loop entry and returns a copy, blocking a change made after preflight. Each
iteration has its own ledger identity. Use a stable, distinct provider
idempotency key per item, as shown with the host-supplied `id` field. The item
list cannot be reassigned, and this slice does not support empty lists, nested
loops, or a branch in the same composite.

Multiple or nested conditions, short-circuit or conditional expressions,
unbounded or nested loops, comprehensions, generators, nested calls such as `outer(inner())`,
early returns, nested definitions, recursion, dynamic dispatch, nested
composites, and unsupported/opaque boundaries are rejected before execution.
This avoids inferring order by sorting every AST call by source location.
Static analysis is preflight assistance, not proof that arbitrary hidden
effects were found. Deterministic local computation may rerun, but time,
randomness, mutable globals, fresh external reads, and other nondeterministic
inputs must not change supported calls or their arguments.

Parent completion requires the current replay to observe every manifest step in
order and to record each child as resolved under the current parent fence.
Admission alone is not completion evidence. A completed child can still be
replayed through its wrapper after a crash before parent completion.

The parent lease renews automatically while the body is running and renewal
failure blocks the next boundary or completion. Use a stable host-owned
operation ID. A new operation ID means a genuinely new invocation; a new
worker, retry attempt, or parent fence does not. The durable
record pins an invocation to its original manifest and definition. Changing
order, adding/removing steps, changing bindings, arguments, or destinations
blocks recovery rather than minting fresh child identities.

SQLite, file, Redis, PostgreSQL, and in-process storage provide the
composite-control capability. Redis and PostgreSQL persist parent records in
their respective atomic state stores with revision-checked updates. Workers
must use the same Redis key prefix or PostgreSQL ledger table and database to
share parent authority. In-memory storage is useful for unit tests only. The
Python decorator remains the only API that statically inspects a function
body. The sidecar also offers an experimental `composite-v1` extension for
explicitly declared straight-line manifests on file or shared PostgreSQL
storage.

## Real-process recovery proof

The recovery protocol above already exists; this proof tests its boundaries,
not a new workflow engine. Run the opt-in suite from `sdk/`:

```sh
pip install -e ".[dev,redis,postgres]"
export MYCELIUM_TEST_COMPOSITE_PROCESS_PROOF=1
export MYCELIUM_TEST_REDIS_URL=redis://127.0.0.1:6379/15
export MYCELIUM_TEST_POSTGRES_DSN=postgresql://mycelium:mycelium@127.0.0.1:5432/mycelium_test
export MYCELIUM_CI_REQUIRE_REDIS=1
export MYCELIUM_CI_REQUIRE_POSTGRES=1
pytest tests/test_composite_process_recovery.py tests/test_backend_gates.py -q -rs
```

Use dedicated test services. Redis keys and PostgreSQL tables are UUID-scoped
and cleaned up without flushing databases. Without the require flags, a missing
driver, unreachable Redis, or unset PostgreSQL DSN skips that backend's cases;
the SQLite cases still run. A configured PostgreSQL connection failure fails
the test. Ordinary SDK tests skip the opt-in process suite.
The `Composite recovery proof` workflow requires both shared backends and runs
on affected runtime/test changes or manual dispatch, not documentation-only PRs.

[The process suite](../tests/test_composite_process_recovery.py) launches
[independent workers](../tests/fixtures/composite_shared_worker.py), kills them
with the OS process-kill operation, and reopens storage in a new process. Lease
renewal remains enabled; suspension tests stop the whole worker, including its
heartbeat threads. A separate SQLite fake provider uses FULL-synchronous
commits and deliberately does not deduplicate effects, so the suite counts
actual provider effects rather than trusting ledger state alone.

| Interruption or contention | Required observation |
| --- | --- |
| Parent admitted a child; child has not claimed yet | Resume executes the unattempted child once |
| Child claimed; provider boundary not crossed | Authoritative non-execution permits recovery |
| Child completed; parent has not recorded resolution | Replay returns the saved receipt and resolves the parent step |
| Completed child or all children completed; parent unfinished | Completed effects do not run again |
| Boundary marked or effect committed; result not stored | Unknown evidence blocks; read-only confirmed evidence permits resolution |
| Missing provider reference | Availability of a lookup adapter does not silently authorize replay |
| Pinned items, loops, or input/result branches | Stored results preserve the original path and iteration identities |
| Changed arguments, order, branch, loop count, or definition | Reject before any new provider effect, including under a reused version label |
| Two resumers | Only the current parent owner progresses |
| Suspended stale worker resumes after takeover | Reject its next admission or provider-boundary crossing; winning result remains intact |

The fake provider's `proven_absent` mode supplies authoritative non-execution
only for this controlled, synchronous model. A real provider's missing search
result, eventual visibility, or expired key is not equivalent evidence. These
tests do not prove power-loss durability, production provider correctness,
arbitrary hidden-effect coverage, or cancellation of an already-sent request.

## Language-neutral sidecar extension

The extension is advertised in `GET /v1/capabilities` under
`extensions.composite-v1`; it does not change the frozen `v1alpha1` effect
routes. Claim a parent at `POST /extensions/composite-v1/composites/claim` with
`operation_id`, a stable `definition`, and an ordered list of unique
`{step_id, tool_id}` pairs. The sidecar pins the manifest and issues a parent
owner and fence. A changed definition, step order, or tool blocks replay.

For each step, call `.../steps/{step_id}/claim` with the parent owner/fence and
the complete child identity and decision. Only `EXECUTE` permits the provider
call. Before the provider boundary, call `.../boundary`; after success, call
`.../complete`. These commands validate both the parent fence and the child
effect fence. The sidecar derives each child effect identity from its ordinary
identity fields plus the parent operation, definition, and step. Reusing the
same tool input in another parent creates a distinct child effect.

On resume, claim the same parent manifest. A completed child returns
`RETURN_STORED_RESULT`; call `.../resolve` to acknowledge its evidence in the
new replay, then continue with the next step. Finish the parent only after all
children are committed and resolved. `.../renew` extends a long-running parent
lease; `.../release` relinquishes it after a controlled interruption. An
unexpected worker death leaves the lease for a later fenced reclaim. `UNKNOWN`
and unresolved children remain blocked.

The host must declare the full sequence before claiming and call only those
steps in order. The sidecar cannot inspect TypeScript or Go program control
flow and does not make arbitrary workflows transactional. Its explicit
manifests use a separate namespace and cannot resume a Python `@composite`
decorator invocation. A parent check just before a provider call cannot
cancel an external request already sent.

## Decision record

The implementation chooses a lightweight durable parent-control record plus
ordinary child `LedgerEntry` rows. `handoff_scope()` remains audit causation
only. The parent is not an atomic transaction and never aggregates away child
ambiguity. General workflow scheduling and arbitrary conditions based on
prior child outcomes remain deferred.
