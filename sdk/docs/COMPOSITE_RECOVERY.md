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
the composite namespace, pinned definition, and stable source call-site step
ID; it does not replace scope, dispatch, tool, arguments, destination,
side-effect, agent, policy, or identity-schema fields.

On replay the unchanged function runs from the beginning. A completed child
returns its durably serialized and faithfully reconstructable result, while an unattempted child is admitted
under the current parent fence and executes through its existing ledger
protocol. Ambiguous children still reconcile or hard-block. Parent authority
is checked at admission and immediately before the child body and
`mark_maybe_crossed()` boundary. This protects progression but cannot cancel
an external request already sent during a check-to-send race.

The initial scope is an explicit straight-line syntax subset: assignments,
expression statements, and one final return, with each supported call at a
statement boundary. Conditionals, short-circuit or conditional expressions,
loops, comprehensions, generators, nested calls such as `outer(inner())`,
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

SQLite, file, and in-process storage provide the composite-control capability
in this initial implementation. Other backends fail explicitly when composite
mode is requested. In-memory storage is useful for unit tests only.

## Decision record

The implementation chooses a lightweight durable parent-control record plus
ordinary child `LedgerEntry` rows. `handoff_scope()` remains audit causation
only. The parent is not an atomic transaction and never aggregates away child
ambiguity. Straight-line support is intentional; general workflow scheduling
and conditional-path manifests are deferred until enforceable semantics exist.
