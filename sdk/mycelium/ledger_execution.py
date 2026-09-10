"""Sync and async execution orchestration for ledgered tool calls."""

from __future__ import annotations

import inspect
import logging
import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar

from mycelium.ledger_context import (
    _active_transition_var,
    _ActiveTransition,
    _lease_auto_renew,
    _outcome_reexec_authorized,
)
from mycelium.ledger_identity import (
    _claim_kwargs,
    _drop_ledger_keys,
    _is_read_only_binding,
    _use_boundary_call_mapping,
)
from mycelium.ledger_model import (
    LedgerEntry,
    LedgerError,
    LedgerHardBlockError,
    LedgerOutcomeAlreadySetError,
    LedgerSoftBlockError,
)
from mycelium.transition import (
    SideEffectBoundary,
    TerminalOutcome,
    ToolTransitionBinding,
    derive_transition_key_for_call,
    should_propagate_effect_id_as_provider_key,
)

if TYPE_CHECKING:
    from mycelium.action_ledger import ActionLedger
    from mycelium.audit_receipt import AuditReceiptEmitter

P = ParamSpec("P")
R = TypeVar("R")

# Preserve the historical logger namespace after extracting implementation.
_logger = logging.getLogger("mycelium.action_ledger")

def _ledger_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _emit_tool_receipt(
    audit_emitter: AuditReceiptEmitter | None,
    ledger: ActionLedger,
    request_id: str,
    *,
    expected_owner: str | None,
    expected_fence: int,
) -> None:
    if audit_emitter is None:
        return
    entry = ledger.get(request_id)
    if entry is None:
        return
    outcome = entry.resolved_terminal_outcome()
    if outcome not in (
        TerminalOutcome.COMPLETED,
        TerminalOutcome.FAILED_BEFORE_EFFECT,
        TerminalOutcome.FAILED_AFTER_EFFECT,
    ):
        return
    receipt = audit_emitter.emit_from_tool_entry(entry)
    ledger.attach_receipt_ref(
        request_id,
        receipt.receipt_id,
        expected_owner=expected_owner,
        expected_fence=expected_fence,
    )



def _claim_for_transition(
    ledger: ActionLedger,
    request_id: str,
    tool_name: str,
    args: tuple[Any, ...],
    clean_kwargs: dict[str, Any],
    transition_binding: ToolTransitionBinding | None,
    composite_effect_id: str | None = None,
) -> LedgerEntry:
    if _is_read_only_binding(transition_binding):
        return ledger.claim_read_only(request_id, tool_name, args, clean_kwargs)
    if transition_binding is not None:
        return ledger.claim_side_effecting(
            request_id,
            tool_name,
            args,
            clean_kwargs,
            transition_binding,
            _effect_id=composite_effect_id,
        )
    return ledger.claim(request_id, tool_name, args, clean_kwargs)


async def _claim_for_transition_async(
    ledger: ActionLedger,
    request_id: str,
    tool_name: str,
    args: tuple[Any, ...],
    clean_kwargs: dict[str, Any],
    transition_binding: ToolTransitionBinding | None,
    composite_effect_id: str | None = None,
) -> LedgerEntry:
    if _is_read_only_binding(transition_binding):
        return await ledger.claim_read_only_async(request_id, tool_name, args, clean_kwargs)
    if transition_binding is not None:
        return await ledger.claim_side_effecting_async(
            request_id,
            tool_name,
            args,
            clean_kwargs,
            transition_binding,
            _effect_id=composite_effect_id,
        )
    return ledger.claim(request_id, tool_name, args, clean_kwargs)


def _record_failure(
    ledger: ActionLedger,
    request_id: str,
    exc: BaseException,
    *,
    _expected_owner: str | None = None,
    _expected_fence: int | None = None,
) -> None:
    """Record a tool failure with the terminal outcome implied by the boundary.

    ``not_crossed`` → ``FAILED_BEFORE_EFFECT`` (safe to retry per policy),
    ``maybe_crossed`` → ``UNKNOWN`` (ambiguous; hard-block for reconcile),
    ``crossed`` → ``FAILED_AFTER_EFFECT`` (effect happened; hard-block).

    When *_expected_owner* / *_expected_fence* are set, the write also fences on
    the stored entry's ``owner`` / ``fence`` (wrapper-path). A stale worker whose
    claim was superseded holds a lower fence and is rejected here.
    """
    entry = ledger.get(request_id)
    boundary = (
        SideEffectBoundary(entry.side_effect_boundary)
        if entry is not None
        else SideEffectBoundary.NOT_CROSSED
    )
    if boundary == SideEffectBoundary.CROSSED:
        ledger.fail(
            request_id,
            exc,
            failed_after_effect=True,
            _expected_owner=_expected_owner,
            _expected_fence=_expected_fence,
        )
    elif boundary == SideEffectBoundary.MAYBE_CROSSED:
        ledger.mark_unknown(
            request_id,
            error=f"{type(exc).__name__}: {exc}",
            _expected_owner=_expected_owner,
            _expected_fence=_expected_fence,
        )
    else:
        ledger.fail(
            request_id,
            exc,
            _expected_owner=_expected_owner,
            _expected_fence=_expected_fence,
        )


def _record_boundary_decision(
    ledger: ActionLedger,
    request_id: str,
    *,
    tool: str,
    args: tuple[Any, ...],
    kwargs: Mapping[str, Any],
    transition_key: str | None,
    auth_decision: Any,
    currency_decision: Any,
    owner: str | None,
    fence: int | None,
) -> Any:
    """Evaluate the registered predicates and stamp the Decision atomically.

    This is the single decision point: run at the ``INTENDED -> ATTEMPTING``
    boundary after the final-boundary checks passed and before body_start. The
    built-in authority + currency predicates read the already-computed
    ``auth_decision`` / ``currency_decision`` (no re-run, no double-enforcement);
    host-registered predicates decide over the same immutable snapshot. The
    result is written under the same fenced CAS as every in-flight mutation, so
    a superseded worker cannot record — or act on — a stale decision.
    """
    from mycelium.decision import (
        Decision,
        DecisionIntent,
        build_snapshot,
        emit_policy_outcomes_after_decision,
        get_decision_engine,
        get_decision_evidence,
    )
    from mycelium.secret_protection import sanitize_for_decision_evidence

    evidence_args, evidence_kwargs = get_decision_evidence(tuple(args), kwargs)
    intent = DecisionIntent(
        tool=tool,
        args=evidence_args,
        kwargs=evidence_kwargs,
        request_id=request_id,
        transition_key=transition_key,
    )
    snapshot = build_snapshot(
        intent,
        authority_decision=auth_decision,
        currency_decision=currency_decision,
    )
    decision = get_decision_engine().evaluate(intent, snapshot)
    decision = Decision.from_dict(sanitize_for_decision_evidence(decision.to_dict()))
    try:
        ledger.record_decision(
            request_id,
            decision.to_dict(),
            expected_owner=owner,
            expected_fence=fence,
        )
    except LedgerOutcomeAlreadySetError:
        _emit_fence_rejection(
            ledger,
            request_id,
            tool=tool,
            error_class="LedgerOutcomeAlreadySetError",
        )
        _logger.warning(
            "could not record decision for %s: transition superseded "
            "(stale fence/owner) — refusing to advance",
            request_id,
        )
        raise
    emit_policy_outcomes_after_decision(tool, request_id)
    if not decision.allowed:
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool,
            event="decision_denial",
            gate="DENY",
            error_class="LedgerHardBlockError",
        )
    return decision


def _emit_fence_rejection(
    ledger: ActionLedger,
    request_id: str,
    *,
    tool: str,
    error_class: str,
) -> None:
    """Best-effort operational outcome for a refused stale transition write."""
    try:
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool,
            event="fence_rejection",
            error_class=error_class,
        )
    except Exception:
        _logger.exception("could not emit fence rejection for %s", request_id)


def _boundary_denial_facts(
    blocked: Exception,
    *,
    authority_offset: int,
    currency_offset: int,
) -> tuple[Any, Any]:
    from mycelium.authority_window import (
        AuthorityExpiredError,
        get_authority_decisions,
    )
    from mycelium.use_time_currency import get_use_time_decisions

    authority = get_authority_decisions()[authority_offset:]
    currency = get_use_time_decisions()[currency_offset:]
    auth_decision = authority[-1] if authority else None
    currency_decision = currency[-1] if currency else None
    denied = SimpleNamespace(
        decision="denied",
        reason=getattr(blocked, "reason", None)
        or getattr(blocked, "violation", None)
        or type(blocked).__name__,
    )
    if isinstance(blocked, AuthorityExpiredError) and auth_decision is None:
        auth_decision = denied
    elif currency_decision is None:
        currency_decision = denied
    return auth_decision, currency_decision


def _raise_denied_decision(request_id: str, decision: Any) -> None:
    if not decision.allowed:
        raise LedgerHardBlockError(
            f"decision denied for {request_id!r}: "
            f"{'; '.join(decision.denied_reasons) or 'policy predicate refused'}"
        )


def _ensure_provider_key_for_execution(
    *,
    ledger: ActionLedger,
    request_id: str,
    transition_binding: ToolTransitionBinding | None,
    claimed_entry: LedgerEntry,
    clean_kwargs: dict[str, Any],
    call_mapping: dict[str, Any],
    owner: str | None,
    fence: int,
) -> LedgerEntry:
    """Inject and persist provider key from effect_id when policy requests it."""
    if transition_binding is None:
        return claimed_entry
    param = transition_binding.provider_idempotency_key_param
    if param is None:
        return claimed_entry
    if clean_kwargs.get(param) is not None:
        return claimed_entry
    if not should_propagate_effect_id_as_provider_key(transition_binding):
        return claimed_entry
    provider_key = claimed_entry.provider_idempotency_key or claimed_entry.effect_id
    if provider_key is None:
        raise LedgerError(
            f"Cannot derive provider idempotency key for {request_id!r}: missing effect_id"
        )
    updated = ledger.attach_provider_idempotency_key(
        request_id,
        provider_key,
        expected_owner=owner,
        expected_fence=fence,
    )
    clean_kwargs[param] = provider_key
    call_mapping[param] = provider_key
    active = _active_transition_var.get()
    if active is not None and active.request_id == request_id:
        _active_transition_var.set(
            replace(
                active,
                call_kwargs={**dict(active.call_kwargs), param: provider_key},
            )
        )
    return updated


def _identity_lookup_kwargs(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Merge bound positional names so ``request_id_from`` can see them."""
    merged = dict(kwargs)
    try:
        bound = inspect.signature(func).bind_partial(*args, **kwargs)
        for name, value in bound.arguments.items():
            if name not in merged and name not in {"args", "kwargs"}:
                merged[name] = value
    except (TypeError, ValueError):
        return merged
    return merged


def _run_ledgered(
    func: Callable[P, R],
    tool_name: str,
    ledger: ActionLedger,
    args: P.args,
    kwargs: P.kwargs,
    audit_emitter: AuditReceiptEmitter | None = None,
    transition_binding: ToolTransitionBinding | None = None,
) -> R:
    from mycelium.composite import get_active_composite

    composite = get_active_composite()
    composite_child = (
        composite.prepare_child(tool_name, args, kwargs, transition_binding)
        if composite is not None and transition_binding is not None
        else None
    )
    identity_kwargs = _identity_lookup_kwargs(func, args, kwargs)
    request_id = ledger.derive_request_id(
        tool_name,
        args,
        kwargs,
        transition_binding=transition_binding,
        identity_kwargs=identity_kwargs,
    )
    clean_kwargs = _drop_ledger_keys(kwargs)
    claim_kwargs = _claim_kwargs(kwargs, clean_kwargs)
    _outcome_reexec_authorized.set(False)
    try:
        existing = _claim_for_transition(
            ledger,
            request_id,
            tool_name,
            args,
            claim_kwargs,
            transition_binding,
            composite_child.effect_id if composite_child is not None else None,
        )
    except LedgerHardBlockError:
        try:
            ledger._emit_outcome(
                request_id=request_id,
                tool=tool_name,
                event="resolution",
                gate="HARD_BLOCK",
                error_class="LedgerHardBlockError",
            )
        except Exception:
            _logger.exception(
                "could not emit HARD_BLOCK outcome for %s; original ledger error follows",
                request_id,
            )
        raise
    except LedgerSoftBlockError:
        try:
            ledger._emit_outcome(
                request_id=request_id,
                tool=tool_name,
                event="resolution",
                gate="SOFT_BLOCK",
                error_class="LedgerSoftBlockError",
            )
        except Exception:
            _logger.exception(
                "could not emit SOFT_BLOCK outcome for %s; original ledger error follows",
                request_id,
            )
        raise
    request_id = existing.request_id
    if existing.is_terminal_completed():
        if composite is not None and composite_child is not None:
            composite.resolve_child(composite_child.step_id)
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="resolution",
            gate="RETURN",
            terminal_outcome=TerminalOutcome.COMPLETED,
        )
        return existing.result

    owner = _ledger_owner()
    fence = existing.fence
    authorized_reexec = _outcome_reexec_authorized.get()
    side_effect_class = (
        transition_binding.side_effect_class if transition_binding is not None else None
    )
    ledger._emit_outcome(
        request_id=request_id,
        tool=tool_name,
        event="resolution",
        gate="ALLOW",
        terminal_outcome=TerminalOutcome.IN_FLIGHT,
        side_effect_class=side_effect_class,
        authorized_reexec=authorized_reexec,
        owner=owner,
    )

    call_mapping = _use_boundary_call_mapping(func, args, clean_kwargs, kwargs)
    token = _active_transition_var.set(
        _ActiveTransition(
            ledger,
            request_id,
            transition_binding,
            call_mapping,
            owner,
            fence,
        )
    )
    try:
        from mycelium.authority_window import (
            AuthorityExpiredError,
            get_authority_decisions,
        )
        from mycelium.decision import finalize_policy_facts_at_boundary
        from mycelium.use_time_currency import (
            UseTimeCurrencyError,
            enforce_use_boundary,
            get_use_time_decisions,
        )

        finalize_policy_facts_at_boundary()
        blocked: AuthorityExpiredError | UseTimeCurrencyError | None = None
        authority_offset = len(get_authority_decisions())
        currency_offset = len(get_use_time_decisions())
        try:
            auth_decision, currency_decision = enforce_use_boundary(kwargs=call_mapping)
        except (AuthorityExpiredError, UseTimeCurrencyError) as exc:
            blocked = exc
            auth_decision, currency_decision = _boundary_denial_facts(
                blocked,
                authority_offset=authority_offset,
                currency_offset=currency_offset,
            )
            event = (
                "use_time_currency"
                if isinstance(blocked, UseTimeCurrencyError)
                else "authority_window"
            )
            try:
                ledger._emit_outcome(
                    request_id=request_id,
                    tool=tool_name,
                    event=event,
                    gate="DENY",
                    terminal_outcome=TerminalOutcome.FAILED_BEFORE_EFFECT,
                    side_effect_class=side_effect_class,
                    tool_body_executed=False,
                    authorized_reexec=authorized_reexec,
                    owner=owner,
                    error_class=type(blocked).__name__,
                    policy_version=(
                        transition_binding.policy_version
                        if transition_binding is not None
                        else None
                    ),
                )
            except Exception:
                _logger.exception(
                    "could not emit %s denial for %s",
                    event,
                    request_id,
                )

        if getattr(auth_decision, "decision", "skipped") == "allowed":
            try:
                ledger._emit_outcome(
                    request_id=request_id,
                    tool=tool_name,
                    event="authority_window",
                    gate="ALLOW",
                    terminal_outcome=TerminalOutcome.IN_FLIGHT,
                    side_effect_class=side_effect_class,
                    tool_body_executed=False,
                    authorized_reexec=authorized_reexec,
                    owner=owner,
                    policy_version=getattr(auth_decision, "policy_version", None)
                    or (
                        transition_binding.policy_version
                        if transition_binding is not None
                        else None
                    ),
                )
            except Exception:
                _logger.exception(
                    "could not emit authority_window allow for %s",
                    request_id,
                )

        if getattr(currency_decision, "decision", "skipped") == "allowed":
            try:
                ledger._emit_outcome(
                    request_id=request_id,
                    tool=tool_name,
                    event="use_time_currency",
                    gate="ALLOW",
                    terminal_outcome=TerminalOutcome.IN_FLIGHT,
                    side_effect_class=side_effect_class,
                    tool_body_executed=False,
                    authorized_reexec=authorized_reexec,
                    owner=owner,
                    policy_version=currency_decision.policy_version
                    or (
                        transition_binding.policy_version
                        if transition_binding is not None
                        else None
                    ),
                )
            except Exception:
                _logger.exception(
                    "could not emit use_time_currency allow for %s",
                    request_id,
                )

        decision = _record_boundary_decision(
            ledger,
            request_id,
            tool=tool_name,
            args=args,
            kwargs=call_mapping,
            transition_key=(
                derive_transition_key_for_call(tool_name, args, dict(kwargs), transition_binding)
                if transition_binding is not None
                else None
            ),
            auth_decision=auth_decision,
            currency_decision=currency_decision,
            owner=owner,
            fence=fence,
        )
        if blocked is not None:
            raise blocked
        from mycelium.decision import get_policy_blocked_error

        policy_blocked = get_policy_blocked_error()
        if policy_blocked is not None:
            raise policy_blocked
        _raise_denied_decision(request_id, decision)
        existing = _ensure_provider_key_for_execution(
            ledger=ledger,
            request_id=request_id,
            transition_binding=transition_binding,
            claimed_entry=existing,
            clean_kwargs=clean_kwargs,
            call_mapping=call_mapping,
            owner=owner,
            fence=fence,
        )

        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="body_start",
            terminal_outcome=TerminalOutcome.IN_FLIGHT,
            side_effect_class=side_effect_class,
            tool_body_executed=True,
            authorized_reexec=authorized_reexec,
            owner=owner,
        )

        from mycelium.secret_protection import (
            get_active_secret_policy,
            resolve_declared_secret_fields,
        )

        policy = get_active_secret_policy()
        extra = policy.secret_fields if policy is not None else frozenset()
        exec_args, exec_kwargs = resolve_declared_secret_fields(
            func, args, clean_kwargs, extra_fields=extra
        )
        with _lease_auto_renew(
            ledger,
            request_id,
            tool=tool_name,
            owner=owner,
            fence=fence,
        ):
            if composite is not None:
                composite.validate_boundary()
            result = func(*exec_args, **exec_kwargs)
            if composite is not None and composite_child is not None:
                composite.validate_result(result, step_id=composite_child.step_id)
    except (AuthorityExpiredError, UseTimeCurrencyError) as blocked:
        try:
            _record_failure(
                ledger, request_id, blocked, _expected_owner=owner, _expected_fence=fence
            )
            _emit_tool_receipt(
                audit_emitter,
                ledger,
                request_id,
                expected_owner=owner,
                expected_fence=fence,
            )
        except LedgerOutcomeAlreadySetError:
            pass
        except Exception:
            _logger.exception(
                "could not record use-boundary denial for %s",
                request_id,
            )
        raise
    except Exception as exc:
        from mycelium.secret_protection import (
            get_active_secret_policy,
        )
        from mycelium.secret_protection import (
            sanitize_exception as _sanitize_exc,
        )

        policy = get_active_secret_policy()
        if policy is not None and policy.enabled:
            exc = _sanitize_exc(exc)
        # A storage failure while recording the failure must not mask the
        # original tool exception — log it, then re-raise the tool's own error.
        # An outcome-already-set error also does not mask — the transition was
        # resolved elsewhere after the tool started.
        try:
            _record_failure(ledger, request_id, exc, _expected_owner=owner, _expected_fence=fence)
            _emit_tool_receipt(
                audit_emitter,
                ledger,
                request_id,
                expected_owner=owner,
                expected_fence=fence,
            )
        except LedgerOutcomeAlreadySetError:
            _logger.warning(
                "outcome already set for %s while recording failure "
                "(transition resolved elsewhere after tool started) — "
                "re-raising original exception",
                request_id,
            )
        except Exception:
            _logger.exception(
                "could not record failure for %s (storage down?); original tool error follows",
                request_id,
            )
        try:
            ledger._emit_outcome(
                request_id=request_id,
                tool=tool_name,
                event="body_fail",
                side_effect_class=side_effect_class,
                authorized_reexec=authorized_reexec,
                owner=owner,
                error_class=type(exc).__name__,
                policy_version=(
                    transition_binding.policy_version if transition_binding is not None else None
                ),
            )
        except Exception:
            _logger.exception(
                "could not emit body_fail outcome for %s; original tool error follows",
                request_id,
            )
        raise exc
    finally:
        _active_transition_var.reset(token)

    try:
        ledger.complete(request_id, result, _expected_owner=owner, _expected_fence=fence)
        complete_ok = True
    except LedgerOutcomeAlreadySetError:
        _emit_fence_rejection(
            ledger,
            request_id,
            tool=tool_name,
            error_class="LedgerOutcomeAlreadySetError",
        )
        _logger.warning(
            "outcome already set for %s while completing "
            "(transition resolved elsewhere after tool started) — "
            "tool result discarded",
            request_id,
        )
        complete_ok = False
    _emit_tool_receipt(
        audit_emitter,
        ledger,
        request_id,
        expected_owner=owner,
        expected_fence=fence,
    )
    if complete_ok and composite is not None and composite_child is not None:
        composite.resolve_child(composite_child.step_id)
    ledger._emit_outcome(
        request_id=request_id,
        tool=tool_name,
        event="body_complete" if complete_ok else "body_fail",
        side_effect_class=side_effect_class,
        authorized_reexec=authorized_reexec,
        owner=owner,
        error_class=None if complete_ok else "LedgerOutcomeAlreadySetError",
        policy_version=(
            transition_binding.policy_version if transition_binding is not None else None
        ),
    )
    return result

async def _run_ledgered_async(
    func: Callable[P, Awaitable[R]],
    tool_name: str,
    ledger: ActionLedger,
    args: P.args,
    kwargs: P.kwargs,
    audit_emitter: AuditReceiptEmitter | None = None,
    transition_binding: ToolTransitionBinding | None = None,
) -> R:
    from mycelium.composite import get_active_composite

    composite = get_active_composite()
    composite_child = (
        composite.prepare_child(tool_name, args, kwargs, transition_binding)
        if composite is not None and transition_binding is not None
        else None
    )
    identity_kwargs = _identity_lookup_kwargs(func, args, kwargs)
    request_id = ledger.derive_request_id(
        tool_name,
        args,
        kwargs,
        transition_binding=transition_binding,
        identity_kwargs=identity_kwargs,
    )
    clean_kwargs = _drop_ledger_keys(kwargs)
    claim_kwargs = _claim_kwargs(kwargs, clean_kwargs)
    _outcome_reexec_authorized.set(False)
    try:
        existing = await _claim_for_transition_async(
            ledger,
            request_id,
            tool_name,
            args,
            claim_kwargs,
            transition_binding,
            composite_child.effect_id if composite_child is not None else None,
        )
    except LedgerHardBlockError:
        try:
            ledger._emit_outcome(
                request_id=request_id,
                tool=tool_name,
                event="resolution",
                gate="HARD_BLOCK",
                error_class="LedgerHardBlockError",
            )
        except Exception:
            _logger.exception(
                "could not emit HARD_BLOCK outcome for %s; original ledger error follows",
                request_id,
            )
        raise
    except LedgerSoftBlockError:
        try:
            ledger._emit_outcome(
                request_id=request_id,
                tool=tool_name,
                event="resolution",
                gate="SOFT_BLOCK",
                error_class="LedgerSoftBlockError",
            )
        except Exception:
            _logger.exception(
                "could not emit SOFT_BLOCK outcome for %s; original ledger error follows",
                request_id,
            )
        raise
    request_id = existing.request_id
    if existing.is_terminal_completed():
        if composite is not None and composite_child is not None:
            composite.resolve_child(composite_child.step_id)
        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="resolution",
            gate="RETURN",
            terminal_outcome=TerminalOutcome.COMPLETED,
        )
        return existing.result

    owner = _ledger_owner()
    fence = existing.fence
    authorized_reexec = _outcome_reexec_authorized.get()
    side_effect_class = (
        transition_binding.side_effect_class if transition_binding is not None else None
    )
    ledger._emit_outcome(
        request_id=request_id,
        tool=tool_name,
        event="resolution",
        gate="ALLOW",
        terminal_outcome=TerminalOutcome.IN_FLIGHT,
        side_effect_class=side_effect_class,
        authorized_reexec=authorized_reexec,
        owner=owner,
    )

    call_mapping = _use_boundary_call_mapping(func, args, clean_kwargs, kwargs)
    token = _active_transition_var.set(
        _ActiveTransition(
            ledger,
            request_id,
            transition_binding,
            call_mapping,
            owner,
            fence,
        )
    )
    try:
        from mycelium.authority_window import (
            AuthorityExpiredError,
            get_authority_decisions,
        )
        from mycelium.decision import finalize_policy_facts_at_boundary
        from mycelium.use_time_currency import (
            UseTimeCurrencyError,
            enforce_use_boundary_async,
            get_use_time_decisions,
        )

        finalize_policy_facts_at_boundary()
        blocked: AuthorityExpiredError | UseTimeCurrencyError | None = None
        authority_offset = len(get_authority_decisions())
        currency_offset = len(get_use_time_decisions())
        try:
            auth_decision, currency_decision = await enforce_use_boundary_async(kwargs=call_mapping)
        except (AuthorityExpiredError, UseTimeCurrencyError) as exc:
            blocked = exc
            auth_decision, currency_decision = _boundary_denial_facts(
                blocked,
                authority_offset=authority_offset,
                currency_offset=currency_offset,
            )
            event = (
                "use_time_currency"
                if isinstance(blocked, UseTimeCurrencyError)
                else "authority_window"
            )
            try:
                ledger._emit_outcome(
                    request_id=request_id,
                    tool=tool_name,
                    event=event,
                    gate="DENY",
                    terminal_outcome=TerminalOutcome.FAILED_BEFORE_EFFECT,
                    side_effect_class=side_effect_class,
                    tool_body_executed=False,
                    authorized_reexec=authorized_reexec,
                    owner=owner,
                    error_class=type(blocked).__name__,
                    policy_version=(
                        transition_binding.policy_version
                        if transition_binding is not None
                        else None
                    ),
                )
            except Exception:
                _logger.exception(
                    "could not emit %s denial for %s",
                    event,
                    request_id,
                )

        if getattr(auth_decision, "decision", "skipped") == "allowed":
            try:
                ledger._emit_outcome(
                    request_id=request_id,
                    tool=tool_name,
                    event="authority_window",
                    gate="ALLOW",
                    terminal_outcome=TerminalOutcome.IN_FLIGHT,
                    side_effect_class=side_effect_class,
                    tool_body_executed=False,
                    authorized_reexec=authorized_reexec,
                    owner=owner,
                    policy_version=getattr(auth_decision, "policy_version", None)
                    or (
                        transition_binding.policy_version
                        if transition_binding is not None
                        else None
                    ),
                )
            except Exception:
                _logger.exception(
                    "could not emit authority_window allow for %s",
                    request_id,
                )

        if getattr(currency_decision, "decision", "skipped") == "allowed":
            try:
                ledger._emit_outcome(
                    request_id=request_id,
                    tool=tool_name,
                    event="use_time_currency",
                    gate="ALLOW",
                    terminal_outcome=TerminalOutcome.IN_FLIGHT,
                    side_effect_class=side_effect_class,
                    tool_body_executed=False,
                    authorized_reexec=authorized_reexec,
                    owner=owner,
                    policy_version=currency_decision.policy_version
                    or (
                        transition_binding.policy_version
                        if transition_binding is not None
                        else None
                    ),
                )
            except Exception:
                _logger.exception(
                    "could not emit use_time_currency allow for %s",
                    request_id,
                )

        decision = _record_boundary_decision(
            ledger,
            request_id,
            tool=tool_name,
            args=args,
            kwargs=call_mapping,
            transition_key=(
                derive_transition_key_for_call(tool_name, args, dict(kwargs), transition_binding)
                if transition_binding is not None
                else None
            ),
            auth_decision=auth_decision,
            currency_decision=currency_decision,
            owner=owner,
            fence=fence,
        )
        if blocked is not None:
            raise blocked
        from mycelium.decision import get_policy_blocked_error

        policy_blocked = get_policy_blocked_error()
        if policy_blocked is not None:
            raise policy_blocked
        _raise_denied_decision(request_id, decision)
        existing = _ensure_provider_key_for_execution(
            ledger=ledger,
            request_id=request_id,
            transition_binding=transition_binding,
            claimed_entry=existing,
            clean_kwargs=clean_kwargs,
            call_mapping=call_mapping,
            owner=owner,
            fence=fence,
        )

        ledger._emit_outcome(
            request_id=request_id,
            tool=tool_name,
            event="body_start",
            terminal_outcome=TerminalOutcome.IN_FLIGHT,
            side_effect_class=side_effect_class,
            tool_body_executed=True,
            authorized_reexec=authorized_reexec,
            owner=owner,
        )

        from mycelium.secret_protection import (
            get_active_secret_policy,
            resolve_declared_secret_fields,
        )

        policy = get_active_secret_policy()
        extra = policy.secret_fields if policy is not None else frozenset()
        exec_args, exec_kwargs = resolve_declared_secret_fields(
            func, args, clean_kwargs, extra_fields=extra
        )
        with _lease_auto_renew(
            ledger,
            request_id,
            tool=tool_name,
            owner=owner,
            fence=fence,
        ):
            if composite is not None:
                composite.validate_boundary()
            result = await func(*exec_args, **exec_kwargs)
            if composite is not None and composite_child is not None:
                composite.validate_result(result, step_id=composite_child.step_id)
    except (AuthorityExpiredError, UseTimeCurrencyError) as blocked:
        try:
            _record_failure(
                ledger, request_id, blocked, _expected_owner=owner, _expected_fence=fence
            )
            _emit_tool_receipt(
                audit_emitter,
                ledger,
                request_id,
                expected_owner=owner,
                expected_fence=fence,
            )
        except LedgerOutcomeAlreadySetError:
            pass
        except Exception:
            _logger.exception(
                "could not record use-boundary denial for %s",
                request_id,
            )
        raise
    except Exception as exc:
        from mycelium.secret_protection import (
            get_active_secret_policy,
        )
        from mycelium.secret_protection import (
            sanitize_exception as _sanitize_exc,
        )

        policy = get_active_secret_policy()
        if policy is not None and policy.enabled:
            exc = _sanitize_exc(exc)
        # A storage failure while recording the failure must not mask the
        # original tool exception — log it, then re-raise the tool's own error.
        # An outcome-already-set error also does not mask — the transition was
        # resolved elsewhere after the tool started.
        try:
            _record_failure(ledger, request_id, exc, _expected_owner=owner, _expected_fence=fence)
            _emit_tool_receipt(
                audit_emitter,
                ledger,
                request_id,
                expected_owner=owner,
                expected_fence=fence,
            )
        except LedgerOutcomeAlreadySetError:
            _logger.warning(
                "outcome already set for %s while recording failure "
                "(transition resolved elsewhere after tool started) — "
                "re-raising original exception",
                request_id,
            )
        except Exception:
            _logger.exception(
                "could not record failure for %s (storage down?); original tool error follows",
                request_id,
            )
        try:
            ledger._emit_outcome(
                request_id=request_id,
                tool=tool_name,
                event="body_fail",
                side_effect_class=side_effect_class,
                authorized_reexec=authorized_reexec,
                owner=owner,
                error_class=type(exc).__name__,
                policy_version=(
                    transition_binding.policy_version if transition_binding is not None else None
                ),
            )
        except Exception:
            _logger.exception(
                "could not emit body_fail outcome for %s; original tool error follows",
                request_id,
            )
        raise exc
    finally:
        _active_transition_var.reset(token)

    try:
        ledger.complete(request_id, result, _expected_owner=owner, _expected_fence=fence)
        complete_ok = True
    except LedgerOutcomeAlreadySetError:
        _emit_fence_rejection(
            ledger,
            request_id,
            tool=tool_name,
            error_class="LedgerOutcomeAlreadySetError",
        )
        _logger.warning(
            "outcome already set for %s while completing "
            "(transition resolved elsewhere after tool started) — "
            "tool result discarded",
            request_id,
        )
        complete_ok = False
    _emit_tool_receipt(
        audit_emitter,
        ledger,
        request_id,
        expected_owner=owner,
        expected_fence=fence,
    )
    if complete_ok and composite is not None and composite_child is not None:
        composite.resolve_child(composite_child.step_id)
    ledger._emit_outcome(
        request_id=request_id,
        tool=tool_name,
        event="body_complete" if complete_ok else "body_fail",
        side_effect_class=side_effect_class,
        authorized_reexec=authorized_reexec,
        owner=owner,
        error_class=None if complete_ok else "LedgerOutcomeAlreadySetError",
        policy_version=(
            transition_binding.policy_version if transition_binding is not None else None
        ),
    )
    return result
