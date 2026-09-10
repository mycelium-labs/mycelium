"""Durable, straight-line composite effect recovery.

This module deliberately composes the existing action ledger.  A composite is
not a transaction: the parent owns the manifest and execution fence while each
child remains an ordinary, independently reconcilable ledger entry.
"""

# Identity preimages and diagnostic strings are intentionally readable.
# ruff: noqa: E501

from __future__ import annotations

import ast
import functools
import hashlib
import inspect
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypeVar, cast

from mycelium.ledger_model import LedgerError
from mycelium.transition import (
    SideEffectClass,
    ToolTransitionBinding,
    canonical_json,
    derive_effect_id_for_call,
)

R = TypeVar("R")


class CompositeError(LedgerError):
    """Base error for a composite preflight or protocol refusal."""


class CompositeUnsupportedError(CompositeError):
    """The requested composite cannot be safely represented or stored."""


class CompositeDefinitionDriftError(CompositeError):
    """An invocation is being replayed against a different definition."""


class CompositeAuthorityError(CompositeError):
    """The worker no longer owns the parent fence."""


class CompositeBusyError(CompositeAuthorityError):
    """Another live worker owns the parent lease."""


@dataclass(frozen=True)
class CompositeStep:
    step_id: str
    tool: str
    source: str
    binding_digest: str

    def to_dict(self) -> dict[str, str]:
        return {
            "step_id": self.step_id,
            "tool": self.tool,
            "source": self.source,
            "binding_digest": self.binding_digest,
        }


@dataclass(frozen=True)
class CompositeManifest:
    function: str
    definition: str
    steps: tuple[CompositeStep, ...]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "function": self.function,
            "definition": self.definition,
            "steps": [step.to_dict() for step in self.steps],
            "digest": self.digest,
        }


@dataclass(frozen=True)
class _PreparedChild:
    step_id: str
    effect_id: str


def _owner() -> str:
    # Host and PID are useful diagnostics, but are not an ownership identity:
    # two invocations routinely share both.  The invocation token must remain
    # unique for the lifetime of this process and is persisted in the parent
    # record through the owner/fence CAS protocol.
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex}"


def _binding_digest(binding: ToolTransitionBinding | None) -> str:
    if binding is None:
        return "unclassified"
    payload = {
        "side_effect_class": binding.side_effect_class.value,
        "agent_id": binding.agent_id,
        "policy_version": binding.policy_version,
        "scope_from": binding.scope_from,
        "destination": getattr(binding, "destination", None),
        "provider_idempotency_key_param": binding.provider_idempotency_key_param,
    }
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def register_composite_boundary(func: Callable[..., Any]) -> Callable[..., Any]:
    """Register a callable already wrapped by ``ledger``/``ledger_sync``.

    Registration is only metadata.  Interception still occurs through the
    actual wrapper attached to the callable; registering an unrelated alias is
    rejected during manifest construction.
    """
    if not getattr(func, "_mycelium_ledger", False):
        raise CompositeUnsupportedError(
            f"{func!r} is not an intercepted Mycelium ledger callable; decorate "
            "the callable with ledger()/ledger_sync() first"
        )
    _BOUNDARIES[_callable_name(func)] = func
    return func


_BOUNDARIES: dict[str, Callable[..., Any]] = {}
_HELPERS: set[str] = set()


def _callable_name(func: Callable[..., Any]) -> str:
    return f"{getattr(func, '__module__', '')}.{getattr(func, '__qualname__', getattr(func, '__name__', ''))}"


def register_composite_helper(func: Callable[..., Any]) -> Callable[..., Any]:
    """Allow a deterministic, non-effect helper in a composite body.

    This is deliberately explicit.  A helper is not intercepted or protected;
    registering it only says that its execution is local and deterministic for
    replay.  External effects must use a registered ledger boundary instead.
    """
    _HELPERS.add(_callable_name(func))
    return func


def _resolve_value(node: ast.Call, namespace: dict[str, Any]) -> Any:
    value: Any = None
    if isinstance(node.func, ast.Name):
        value = namespace.get(node.func.id)
    elif isinstance(node.func, ast.Attribute):
        chain: list[str] = []
        cur: ast.AST = node.func
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            value = namespace.get(cur.id)
            for attr in chain[::-1]:
                value = getattr(value, attr, None)
                if value is None:
                    break
    return value


def _resolve_call(node: ast.Call, namespace: dict[str, Any]) -> Callable[..., Any] | None:
    value = _resolve_value(node, namespace)
    if callable(value) and getattr(value, "_mycelium_ledger", False):
        return value
    parts: list[str] = []
    if isinstance(node.func, ast.Name):
        parts = [node.func.id]
    elif isinstance(node.func, ast.Attribute):
        chain: list[str] = []
        cur: ast.AST = node.func
        while isinstance(cur, ast.Attribute):
            chain.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts = [cur.id, *reversed(chain)]
    dotted = ".".join(parts)
    return _BOUNDARIES.get(dotted) or next(
        (candidate for key, candidate in _BOUNDARIES.items() if key.endswith(f".{dotted}")),
        None,
    )


_ALLOWED_STATEMENTS = (ast.Expr, ast.Assign, ast.AnnAssign, ast.Return, ast.Pass)
_UNSUPPORTED_EXECUTABLE_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.Match,
    ast.IfExp,
    ast.BoolOp,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
    ast.Lambda,
    ast.Yield,
    ast.YieldFrom,
    ast.NamedExpr,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
)


def _straight_line_body(func: Callable[..., Any], parsed: ast.Module) -> list[ast.stmt]:
    definitions = [
        node for node in parsed.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == getattr(func, "__name__", "")
    ]
    if not definitions:
        raise CompositeUnsupportedError(
            f"cannot locate the inspected body of {func.__qualname__}; keep the composite source inspectable"
        )
    body = definitions[0].body
    for index, statement in enumerate(body):
        if not isinstance(statement, _ALLOWED_STATEMENTS):
            raise CompositeUnsupportedError(
                f"{func.__qualname__} uses unsupported executable syntax "
                f"{type(statement).__name__} at line {getattr(statement, 'lineno', '?')}; "
                "composites support straight-line assignments, expressions, and a final return only"
            )
        if isinstance(statement, ast.Return) and index != len(body) - 1:
            raise CompositeUnsupportedError(
                f"{func.__qualname__} returns before the end of its body at line {statement.lineno}; "
                "early returns are unsupported because they can omit required child steps"
            )
        for node in ast.walk(statement):
            if isinstance(node, _UNSUPPORTED_EXECUTABLE_NODES):
                raise CompositeUnsupportedError(
                    f"{func.__qualname__} uses unsupported executable syntax "
                    f"{type(node).__name__} at line {getattr(node, 'lineno', '?')}; "
                    "conditional expressions, short-circuit expressions, comprehensions, "
                    "generators, lambdas, and nested definitions are not supported"
                )
    return body


def _direct_call(statement: ast.stmt) -> ast.Call | None:
    expression: ast.expr | None
    if isinstance(statement, ast.Expr):
        expression = statement.value
    elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
        expression = statement.value
    elif isinstance(statement, ast.Return):
        expression = statement.value
    else:
        expression = None
    if expression is None:
        return None
    if isinstance(expression, ast.Await):
        expression = expression.value
    calls = [node for node in ast.walk(expression) if isinstance(node, ast.Call)]
    if not calls:
        return None
    if not isinstance(expression, ast.Call) or len(calls) != 1:
        rendered = ast.unparse(expression) if hasattr(ast, "unparse") else ast.dump(expression)
        raise CompositeUnsupportedError(
            f"nested or embedded calls are unsupported at line {getattr(statement, 'lineno', '?')}: {rendered}; "
            "place each supported effect at a straight-line statement boundary"
        )
    return expression


def _build_manifest(func: Callable[..., Any], definition: str | None) -> CompositeManifest:
    try:
        source = inspect.getsource(inspect.unwrap(func))
        parsed = ast.parse(inspect.cleandoc(source))
        body = _straight_line_body(func, parsed)
    except (OSError, TypeError, SyntaxError) as exc:
        raise CompositeUnsupportedError(
            f"cannot statically inspect {func.__qualname__}; register supported "
            "boundaries explicitly and keep the composite source inspectable"
        ) from exc
    steps: list[CompositeStep] = []
    closure = inspect.getclosurevars(func)
    namespace = {**closure.globals, **closure.nonlocals, **getattr(func, "__globals__", {})}
    for ordinal, statement in enumerate(body):
        node = _direct_call(statement)
        if node is None:
            continue
        child = _resolve_call(node, namespace)
        if child is None:
            candidate = _resolve_value(node, namespace)
            if candidate is not None and _callable_name(candidate) in _HELPERS:
                continue
            rendered = ast.unparse(node) if hasattr(ast, "unparse") else ast.dump(node)
            raise CompositeUnsupportedError(
                f"unresolvable call at line {node.lineno}: {rendered}; register the actual "
                "ledger boundary or explicitly register a deterministic helper"
            )
        binding = getattr(child, "_mycelium_transition_binding", None)
        if binding is None or binding.side_effect_class == SideEffectClass.READ:
            raise CompositeUnsupportedError(
                f"call at line {node.lineno} is not a supported consequential Mycelium boundary"
            )
        binding_digest = _binding_digest(binding)
        source = ast.dump(node, annotate_fields=True, include_attributes=False)
        step_identity = canonical_json(
            {
                "composite": _callable_name(func),
                "ordinal": ordinal,
                "child": _callable_name(child),
                "binding_digest": binding_digest,
            }
        )
        steps.append(
            CompositeStep(
                step_id=hashlib.sha256(step_identity.encode()).hexdigest()[:24],
                tool=child.__name__,
                source=source,
                binding_digest=binding_digest,
            )
        )
    if not steps:
        raise CompositeUnsupportedError(
            f"{func.__qualname__} has no statically resolvable registered ledger boundaries"
        )
    payload = {
        "function": _callable_name(func),
        "definition": definition or "auto:" + hashlib.sha256(inspect.getsource(inspect.unwrap(func)).encode()).hexdigest(),
        "steps": [step.to_dict() for step in steps],
    }
    digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return CompositeManifest(payload["function"], payload["definition"], tuple(steps), digest)


class _ControlStore:
    """Small CAS store for parent records, supported by durable local stores."""

    def __init__(self, storage: Any) -> None:
        self.storage = storage
        self._memory = getattr(storage, "_composite_records", None)
        if self._memory is None and storage.__class__.__name__ == "InMemoryLedgerStorage":
            self._memory = {}
            storage._composite_records = self._memory
        path: Path | None = None
        inner = getattr(storage, "_inner", None)
        if inner is not None:
            path = getattr(inner, "_path", None)
        file = getattr(storage, "_file", None)
        if path is None:
            path = getattr(file, "_path", None)
        self._path = Path(path).with_suffix(Path(path).suffix + ".composites.json") if path else None
        if self._memory is None and self._path is None:
            raise CompositeUnsupportedError(
                f"{type(storage).__name__} has no atomic composite-control capability; "
                "use SQLite, FileLedgerStorage, or InMemoryLedgerStorage"
            )
        self._lock = None
        if self._path is not None:
            from mycelium.storage.json_file import LockedJsonDictFile

            self._lock = LockedJsonDictFile(self._path)
        self._memory_lock = threading.RLock()

    def _mutate(self, fn: Callable[[dict[str, Any]], R]) -> R:
        if self._memory is not None:
            with self._memory_lock:
                return fn(self._memory)
        assert self._lock is not None
        return self._lock.read_modify_write(fn)

    def create_or_load(self, key: str, manifest: CompositeManifest, namespace: str) -> dict[str, Any]:
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            current = data.get(key)
            if current is None:
                current = {
                    "schema_version": 1,
                    "namespace": namespace,
                    "operation_id": key,
                    "definition": manifest.definition,
                    "manifest": manifest.to_dict(),
                    "manifest_digest": manifest.digest,
                    "status": "RUNNING",
                    "owner": None,
                    "lease_until": None,
                    "fence": 0,
                    "next_step": 0,
                    "children": {},
                }
                data[key] = current
            elif current.get("manifest_digest") != manifest.digest or current.get("definition") != manifest.definition:
                raise CompositeDefinitionDriftError(
                    f"composite {key!r} is pinned to definition {current.get('definition')!r}; "
                    f"received {manifest.definition!r} (manifest drift)"
                )
            return dict(current)

        return self._mutate(mutate)

    def acquire(self, key: str, owner: str, lease_ttl: float) -> dict[str, Any]:
        now = time.time()
        def mutate(data: dict[str, Any]) -> dict[str, Any]:
            rec = data[key]
            live = (rec["lease_until"] or 0) > now
            if rec["owner"] not in (None, owner) and live:
                raise CompositeBusyError(f"composite {key!r} is owned by a live worker")
            if rec["owner"] != owner or not live:
                rec["fence"] = int(rec.get("fence", 0)) + 1
            rec["owner"] = owner
            rec["lease_until"] = now + lease_ttl if lease_ttl > 0 else None
            rec["status"] = "RUNNING"
            data[key] = rec
            return dict(rec)
        return self._mutate(mutate)

    def renew(self, key: str, owner: str, fence: int, lease_ttl: float) -> None:
        def mutate(data: dict[str, Any]) -> None:
            rec = data[key]
            # Expiry makes the record reclaimable; acquire() is the atomic act
            # that changes authority. If no contender has advanced the fence,
            # a delayed owner may safely refresh while holding this store lock.
            if rec.get("owner") != owner or int(rec.get("fence", -1)) != fence:
                raise CompositeAuthorityError(f"lost parent authority for {key!r}")
            rec["lease_until"] = time.time() + lease_ttl if lease_ttl > 0 else None
        self._mutate(mutate)

    def admit(
        self,
        key: str,
        owner: str,
        fence: int,
        step: CompositeStep,
        binding: dict[str, Any],
        lease_ttl: float,
    ) -> None:
        def mutate(data: dict[str, Any]) -> None:
            rec = data[key]
            if rec.get("owner") != owner or int(rec.get("fence", -1)) != fence:
                raise CompositeAuthorityError(f"lost parent authority before admitting {step.step_id}")
            rec["lease_until"] = time.time() + lease_ttl
            expected = rec["manifest"]["steps"]
            index = next((i for i, item in enumerate(expected) if item["step_id"] == step.step_id), None)
            next_step = int(rec.get("next_step", 0))
            if index is None or index > next_step:
                raise CompositeDefinitionDriftError(
                    f"unexpected composite step {step.step_id!r}; expected index {next_step}"
                )
            old = rec["children"].get(step.step_id)
            if old is not None:
                comparable_old = {k: v for k, v in old.items() if k != "outcome"}
                if comparable_old != binding:
                    raise CompositeDefinitionDriftError(f"argument or destination drift at step {step.step_id}")
            if index < next_step:
                if old is None:
                    raise CompositeDefinitionDriftError(
                        f"manifest step {step.step_id!r} is missing durable child evidence"
                    )
                return
            rec["children"][step.step_id] = {**binding, "outcome": "ADMITTED"}
            rec["next_step"] = index + 1
        self._mutate(mutate)

    def resolve_child(self, key: str, owner: str, fence: int, step: CompositeStep) -> None:
        """Record child resolution separately from admission under the parent fence."""
        def mutate(data: dict[str, Any]) -> None:
            rec = data[key]
            if rec.get("owner") != owner or int(rec.get("fence", -1)) != fence:
                raise CompositeAuthorityError(f"lost parent authority while resolving {step.step_id}")
            child = rec["children"].get(step.step_id)
            if child is None:
                raise CompositeDefinitionDriftError(f"child {step.step_id!r} was not admitted")
            if child.get("outcome") not in {"ADMITTED", "COMPLETED"}:
                raise CompositeDefinitionDriftError(f"child {step.step_id!r} has invalid outcome evidence")
            child["outcome"] = "COMPLETED"
        self._mutate(mutate)

    def boundary(self, key: str, owner: str, fence: int, lease_ttl: float) -> None:
        def mutate(data: dict[str, Any]) -> None:
            rec = data[key]
            if rec.get("owner") != owner or int(rec.get("fence", -1)) != fence:
                raise CompositeAuthorityError("parent authority was reclaimed before the external-effect boundary")
            rec["lease_until"] = time.time() + lease_ttl
        self._mutate(mutate)

    def finish(
        self,
        key: str,
        owner: str,
        fence: int,
        expected_step_ids: tuple[str, ...],
        observed_step_ids: tuple[str, ...],
        resolved_step_ids: frozenset[str],
    ) -> None:
        def mutate(data: dict[str, Any]) -> None:
            rec = data[key]
            if (
                rec.get("owner") != owner
                or int(rec.get("fence", -1)) != fence
            ):
                raise CompositeAuthorityError("stale worker cannot complete composite")
            if observed_step_ids != expected_step_ids:
                raise CompositeDefinitionDriftError(
                    "current replay did not encounter the manifest steps in the expected order"
                )
            if resolved_step_ids != frozenset(expected_step_ids):
                raise CompositeDefinitionDriftError(
                    "current replay admitted children without resolving every required outcome"
                )
            children = rec.get("children", {})
            if any(children.get(step_id, {}).get("outcome") != "COMPLETED" for step_id in expected_step_ids):
                raise CompositeDefinitionDriftError(
                    "required child outcome evidence is incomplete; parent remains unresolved"
                )
            rec["status"] = "COMPLETED"
            rec["finished_at"] = time.time()
            rec["owner"] = None
            rec["lease_until"] = None
        self._mutate(mutate)

    def release(self, key: str, owner: str, fence: int) -> None:
        """Release a live owner after a body exception without declaring success."""
        def mutate(data: dict[str, Any]) -> None:
            rec = data[key]
            if rec.get("owner") != owner or int(rec.get("fence", -1)) != fence:
                raise CompositeAuthorityError("stale worker cannot release composite ownership")
            rec["owner"] = None
            rec["lease_until"] = None
        self._mutate(mutate)


_active_composite: ContextVar[CompositeInvocation | None] = ContextVar("mycelium_active_composite", default=None)


def get_active_composite() -> CompositeInvocation | None:
    return _active_composite.get()


class CompositeInvocation(AbstractContextManager["CompositeInvocation"]):
    def __init__(
        self,
        storage: Any,
        operation_id: str,
        manifest: CompositeManifest,
        *,
        namespace: str,
        lease_ttl: float,
        renewal_interval: float | None = None,
    ) -> None:
        if lease_ttl <= 0:
            raise CompositeUnsupportedError("composite lease_ttl must be positive")
        if renewal_interval is not None and renewal_interval <= 0:
            raise CompositeUnsupportedError("renewal_interval must be positive")
        self.store = _ControlStore(storage)
        self.key = f"{namespace}:{operation_id}"
        self.manifest = manifest
        self.owner = _owner()
        self.lease_ttl = lease_ttl
        self.renewal_interval = renewal_interval
        self.record = self.store.create_or_load(self.key, manifest, namespace)
        self.record = self.store.acquire(self.key, self.owner, lease_ttl)
        self.fence = int(self.record["fence"])
        self.cursor = 0
        self.observed_steps: list[str] = []
        self.resolved_steps: set[str] = set()
        self._token = None
        self._renew_stop = threading.Event()
        self._renew_thread: threading.Thread | None = None
        self._renewal_error: CompositeAuthorityError | None = None

    def _start_renewal(self) -> None:
        if (
            self.lease_ttl <= 0
            or self._renew_thread is not None
            or self._renew_stop.is_set()
        ):
            return
        try:
            self.renew()
        except Exception as exc:
            self._renewal_error = CompositeAuthorityError(
                f"automatic parent lease renewal failed for {self.key!r}: {exc}"
            )
            return
        interval = self.renewal_interval or max(min(self.lease_ttl / 3.0, 0.25), 0.01)

        def renew_loop() -> None:
            while not self._renew_stop.wait(interval):
                try:
                    self.renew()
                except Exception as exc:  # fail closed at the next boundary
                    self._renewal_error = CompositeAuthorityError(
                        f"automatic parent lease renewal failed for {self.key!r}: {exc}"
                    )
                    self._renew_stop.set()
                    return

        self._renew_thread = threading.Thread(
            target=renew_loop,
            name=f"mycelium-composite-renew-{self.key}",
            daemon=True,
        )
        self._renew_thread.start()

    def _stop_renewal(self) -> None:
        self._renew_stop.set()
        if self._renew_thread is not None and self._renew_thread is not threading.current_thread():
            self._renew_thread.join(timeout=max(self.lease_ttl, 0.1))
            if self._renew_thread.is_alive() and self._renewal_error is None:
                self._renewal_error = CompositeAuthorityError(
                    f"automatic parent lease renewal did not stop cleanly for {self.key!r}"
                )

    def __enter__(self) -> CompositeInvocation:
        if get_active_composite() is not None:
            raise CompositeUnsupportedError("nested composites are not supported")
        self._token = _active_composite.set(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        try:
            self._stop_renewal()
            if exc is None:
                if self._renewal_error is not None:
                    raise self._renewal_error
                self.store.finish(
                    self.key,
                    self.owner,
                    self.fence,
                    tuple(step.step_id for step in self.manifest.steps),
                    tuple(self.observed_steps),
                    frozenset(self.resolved_steps),
                )
            else:
                self.store.release(self.key, self.owner, self.fence)
        finally:
            if self._token is not None:
                _active_composite.reset(self._token)
        return False

    def prepare_child(self, tool: str, args: tuple[Any, ...], kwargs: dict[str, Any], binding: ToolTransitionBinding) -> _PreparedChild:
        if self._renewal_error is not None:
            raise self._renewal_error
        self.validate_boundary()
        if self.cursor >= len(self.manifest.steps):
            raise CompositeDefinitionDriftError("composite executed more child calls than its manifest")
        step = self.manifest.steps[self.cursor]
        if step.tool != tool or step.binding_digest != _binding_digest(binding):
            raise CompositeDefinitionDriftError(
                f"manifest step {self.cursor} expects {step.tool!r}, received {tool!r}; "
                "definition/order drift"
            )
        base_effect = derive_effect_id_for_call(tool, args, kwargs, binding)
        effect_id = "mycelium:effect:v1:" + hashlib.sha256(
            canonical_json({"base": base_effect, "parent": self.key, "definition": self.manifest.definition, "step": step.step_id}).encode()
        ).hexdigest()
        from mycelium.entity_guard import destination_fingerprint, get_active_entity_decision

        binding_record = {
            "effect_id": effect_id,
            "args_fingerprint": hashlib.sha256(canonical_json({"args": args, "kwargs": kwargs}).encode()).hexdigest(),
            "destination": list(destination_fingerprint(get_active_entity_decision())),
        }
        self.store.admit(
            self.key,
            self.owner,
            self.fence,
            step,
            binding_record,
            self.lease_ttl,
        )
        self.cursor += 1
        self.observed_steps.append(step.step_id)
        # Start after the first durable admission so a background-renewal
        # failure cannot race and reject that same child before its boundary.
        self._start_renewal()
        return _PreparedChild(step.step_id, effect_id)

    def resolve_child(self, step_id: str) -> None:
        if not self.observed_steps or self.observed_steps[-1] != step_id:
            raise CompositeDefinitionDriftError(f"child {step_id!r} resolved out of order")
        step = self.manifest.steps[len(self.observed_steps) - 1]
        self.store.resolve_child(self.key, self.owner, self.fence, step)
        self.resolved_steps.add(step_id)

    def validate_boundary(self) -> None:
        self.store.boundary(self.key, self.owner, self.fence, self.lease_ttl)

    def renew(self) -> None:
        """Renew the parent lease while the unchanged body is running."""
        self.store.renew(self.key, self.owner, self.fence, self.lease_ttl)

    def validate_result(self, value: Any, *, step_id: str | None = None) -> None:
        try:
            encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
            restored = json.loads(encoded)
        except (TypeError, ValueError) as exc:
            raise CompositeUnsupportedError(
                f"composite result{f' for step {step_id}' if step_id else ''} is not JSON serializable; "
                "provide a durable faithful representation"
            ) from exc
        if type(value) is not type(restored) or value != restored:
            raise CompositeUnsupportedError(
                f"composite result{f' for step {step_id}' if step_id else ''} cannot be faithfully "
                "reconstructed from durable storage"
            )


def composite(
    storage: Any,
    *,
    operation_id_from: Callable[[tuple[Any, ...], dict[str, Any]], str] | None = None,
    namespace: str = "mycelium",
    definition: str | None = None,
    lease_ttl: float = 3600.0,
    renewal_interval: float | None = None,
) -> Callable[[Callable[..., R]], Callable[..., R]]:
    """Decorate an unchanged straight-line function for child recovery.

    Child calls must be the actual callables wrapped by ``ledger`` or
    ``ledger_sync``.  ``operation_id_from`` must return the same logical ID on
    retries; it must not generate a new random ID per dispatch.
    """
    def decorate(func: Callable[..., R]) -> Callable[..., R]:
        manifest = _build_manifest(func, definition)

        def operation_id(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
            value = operation_id_from(args, kwargs) if operation_id_from else kwargs.get("operation_id")
            if not isinstance(value, str) or not value.strip():
                raise CompositeUnsupportedError(
                    f"{func.__qualname__} requires a stable operation ID via operation_id_from or operation_id=..."
                )
            return value.strip()

        def call_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
            if "operation_id" in kwargs:
                try:
                    inspect.signature(func).bind_partial(**kwargs)
                except TypeError:
                    copied = dict(kwargs)
                    copied.pop("operation_id", None)
                    return copied
            return kwargs

        if inspect.iscoroutinefunction(func):
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> R:
                with CompositeInvocation(
                    storage,
                    operation_id(args, kwargs),
                    manifest,
                    namespace=namespace,
                    lease_ttl=lease_ttl,
                    renewal_interval=renewal_interval,
                ):
                    result = await func(*args, **call_kwargs(kwargs))
                    active = get_active_composite()
                    if active is not None:
                        active.validate_result(result)
                    return result
            setattr(async_wrapper, "_mycelium_composite_manifest", manifest)
            return cast(Callable[..., R], async_wrapper)

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> R:
            with CompositeInvocation(
                storage,
                operation_id(args, kwargs),
                manifest,
                namespace=namespace,
                lease_ttl=lease_ttl,
                renewal_interval=renewal_interval,
            ):
                result = func(*args, **call_kwargs(kwargs))
                active = get_active_composite()
                if active is not None:
                    active.validate_result(result)
                return result
        setattr(sync_wrapper, "_mycelium_composite_manifest", manifest)
        return cast(Callable[..., R], sync_wrapper)
    return decorate


__all__ = [
    "CompositeDefinitionDriftError",
    "CompositeError",
    "CompositeManifest",
    "CompositeStep",
    "CompositeAuthorityError",
    "CompositeBusyError",
    "CompositeUnsupportedError",
    "composite",
    "get_active_composite",
    "register_composite_boundary",
    "register_composite_helper",
]
