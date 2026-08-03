"""LangGraph runtime metadata adapter.

The adapter is deliberately optional: core Mycelium only understands generic
dispatch and execution-scope fields. When enabled from YAML, this module adds a
hidden ``ToolRuntime`` parameter that LangGraph's ``ToolNode`` injects, then
maps its identifiers into those generic fields.
"""

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable
from contextlib import ExitStack
from typing import Any, TypeVar

from mycelium.transition import (
    TransitionScope,
    dispatch_scope,
    execution_scope,
)

R = TypeVar("R")


class LangGraphIntegrationError(RuntimeError):
    """Raised when the optional LangGraph adapter cannot be applied."""


def _load_tool_runtime() -> type[Any]:
    try:
        from langgraph.prebuilt import ToolRuntime
    except ImportError as exc:
        raise LangGraphIntegrationError(
            "LangGraph integration is enabled but LangGraph is not installed; "
            "install 'mycelium-runtime[langgraph]'"
        ) from exc
    return ToolRuntime


def _runtime_metadata(runtime: Any) -> tuple[str | None, TransitionScope]:
    """Map a LangGraph ToolRuntime to generic Mycelium identity fields."""
    config = runtime.config if isinstance(getattr(runtime, "config", None), dict) else {}
    configurable = (
        config.get("configurable", {})
        if isinstance(config.get("configurable"), dict)
        else {}
    )
    metadata = (
        config.get("metadata", {})
        if isinstance(config.get("metadata"), dict)
        else {}
    )
    execution_info = getattr(runtime, "execution_info", None)

    thread_id = (
        getattr(execution_info, "thread_id", None)
        or configurable.get("thread_id")
        or ""
    )
    run_id = getattr(execution_info, "run_id", None) or config.get("run_id") or ""
    node = metadata.get("langgraph_node") or ""
    tool_call_id = getattr(runtime, "tool_call_id", None)

    return (
        str(tool_call_id) if tool_call_id is not None else None,
        TransitionScope(
            thread_id=str(thread_id),
            run_id=str(run_id),
            node=str(node),
        ),
    )


def _signature_with_runtime(
    func: Callable[..., Any],
    runtime_type: type[Any],
) -> inspect.Signature:
    signature = inspect.signature(func)
    if "runtime" in signature.parameters:
        raise LangGraphIntegrationError(
            f"tool {func.__name__!r} already declares a 'runtime' parameter; "
            "automatic LangGraph metadata injection reserves that name"
        )

    runtime_parameter = inspect.Parameter(
        "runtime",
        kind=inspect.Parameter.KEYWORD_ONLY,
        annotation=runtime_type,
    )
    parameters = list(signature.parameters.values())
    for index, parameter in enumerate(parameters):
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            parameters.insert(index, runtime_parameter)
            break
    else:
        parameters.append(runtime_parameter)
    return signature.replace(parameters=parameters)


def instrument_langgraph_tool(
    func: Callable[..., R],
) -> Callable[..., R]:
    """Add trusted LangGraph runtime identity to a Mycelium-wrapped tool.

    Calls outside LangGraph remain valid: when no ``runtime`` kwarg is injected,
    the function runs with normal Mycelium key derivation.
    """
    if getattr(func, "_mycelium_langgraph_integration", False):
        return func

    runtime_type = _load_tool_runtime()
    runtime_signature = _signature_with_runtime(func, runtime_type)
    is_async = inspect.iscoroutinefunction(func)

    if is_async:

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            runtime = kwargs.pop("runtime", None)
            if runtime is None:
                return await func(*args, **kwargs)
            dispatch_id, scope = _runtime_metadata(runtime)
            with ExitStack() as stack:
                stack.enter_context(execution_scope(scope))
                if dispatch_id is not None:
                    stack.enter_context(dispatch_scope(dispatch_id))
                return await func(*args, **kwargs)

        wrapper: Callable[..., R] = async_wrapper
    else:

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            runtime = kwargs.pop("runtime", None)
            if runtime is None:
                return func(*args, **kwargs)
            dispatch_id, scope = _runtime_metadata(runtime)
            with ExitStack() as stack:
                stack.enter_context(execution_scope(scope))
                if dispatch_id is not None:
                    stack.enter_context(dispatch_scope(dispatch_id))
                return func(*args, **kwargs)

        wrapper = sync_wrapper

    wrapper.__signature__ = runtime_signature  # type: ignore[attr-defined]
    wrapper.__annotations__ = {
        **getattr(func, "__annotations__", {}),
        "runtime": runtime_type,
    }
    wrapper._mycelium_langgraph_integration = True  # type: ignore[attr-defined]
    return wrapper


def completion_gate_end(
    contract: Any,
    *,
    config: dict[str, Any] | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
) -> Any:
    """AF-007 adapter: call before a LangGraph END / last node returns.

    Resolves ``run_id`` from an explicit argument, else LangGraph
    ``config["configurable"]`` / ``config["run_id"]``, else active
    ``execution_scope``. Raises ``CompletionRefusedError`` when required
    subtasks are still pending.

    Example (last node)::

        from mycelium import load_config
        from mycelium.integrations.langgraph import completion_gate_end

        contract = load_config("mycelium.yaml").build_completion_contract()

        def finalize(state, config):
            completion_gate_end(contract, config=config)
            return state
    """
    from mycelium.completion_contract import gate_graph_end
    from mycelium.transition import TransitionScope, execution_scope

    cfg = config if isinstance(config, dict) else {}
    configurable = (
        cfg.get("configurable", {})
        if isinstance(cfg.get("configurable"), dict)
        else {}
    )
    resolved_run = (
        run_id
        or cfg.get("run_id")
        or configurable.get("run_id")
        or ""
    )
    resolved_thread = (
        thread_id
        or configurable.get("thread_id")
        or ""
    )
    scope_key = str(resolved_run) if resolved_run else None
    if scope_key is None and resolved_thread:
        scope_key = str(resolved_thread)

    if resolved_run or resolved_thread:
        with execution_scope(
            TransitionScope(
                thread_id=str(resolved_thread),
                run_id=str(resolved_run),
                node="end",
            )
        ):
            return gate_graph_end(contract, scope_key=scope_key)
    return gate_graph_end(contract, scope_key=scope_key)


__all__ = [
    "LangGraphIntegrationError",
    "instrument_langgraph_tool",
    "completion_gate_end",
]
