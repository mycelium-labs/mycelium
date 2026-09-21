#!/usr/bin/env python3
"""LangGraph + Redis + receipts + crash → charge once (adoption example).

Copy this folder. Run from ``sdk/`` (or any env with the package installed):

    docker run -d --name mycelium-redis -p 6379:6379 redis:7
    pip install 'mycelium-runtime[langgraph]' redis
    export MYCELIUM_REDIS_URL=redis://127.0.0.1:6379/15
    export MYCELIUM_SIGNING_KEY=demo-signing-key
    python examples/langgraph_redis_crash/run.py

Shows two partner-facing proofs on a real Redis ledger:

1. **Redispatch RETURN** — same LangGraph tool_call twice → body once + receipt
2. **Crash → HARD_BLOCK** — kill mid-flight worker, redispatch → no second charge
"""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

# Checkout-friendly: import the local SDK without an install.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/15"
ENV_REDIS_URL = "MYCELIUM_REDIS_URL"
ENV_SIGNING_KEY = "MYCELIUM_SIGNING_KEY"


def _redis_url() -> str:
    return os.environ.get(ENV_REDIS_URL) or DEFAULT_REDIS_URL


def redis_reachable(url: str | None = None) -> bool:
    try:
        import redis
    except ImportError:
        return False
    client = redis.Redis.from_url(url or _redis_url(), decode_responses=True)
    try:
        return bool(client.ping())
    except Exception:
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def _cleanup_prefix(url: str, prefix: str) -> None:
    import redis

    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        keys = list(client.scan_iter(match=f"{prefix}*"))
        if keys:
            client.delete(*keys)
    finally:
        client.close()


def _demo_yaml(
    *,
    prefix: str,
    receipts_path: str,
    lease_ttl: float,
    poll_timeout: float,
) -> str:
    """YAML for this run — same shape as mycelium.example.yaml, unique prefix."""
    return f"""
integrations:
  langgraph:
    enabled: true

transition:
  agent_id: langgraph-payment-demo
  policy_version: "2026.08.1"
  lease_ttl: {lease_ttl}
  poll_interval: 0.05
  poll_timeout: {poll_timeout}
  reclaim_requires_death_signal: true

action_ledger:
  storage: redis
  url: {_redis_url()!r}
  prefix: {prefix!r}
  tools:
    - send_payment

audit_receipt:
  signing_key_env: {ENV_SIGNING_KEY}
  storage: file
  path: {receipts_path!r}
  auto: true

tools:
  send_payment:
    side_effect_class: keyed_mutate
    provider_idempotency_key_param: idempotency_key
    audit_receipt: true
"""


def _tool_message(
    amount: float = 10.0,
    call_id: str = "call_1",
    *,
    request_id: str | None = None,
    idempotency_key: str | None = None,
):
    from langchain_core.messages import AIMessage

    args: dict[str, Any] = {"amount": amount}
    if request_id is not None:
        args["request_id"] = request_id
    if idempotency_key is not None:
        args["idempotency_key"] = idempotency_key
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "send_payment",
                "args": args,
                "id": call_id,
                "type": "tool_call",
            }
        ],
    )


def _graph_for(tool: Any):
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([tool]))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    return builder.compile()


def _runtime_config(thread_id: str, run_id: str) -> dict[str, Any]:
    return {
        "configurable": {"thread_id": thread_id},
        "run_id": run_id,
    }


def scenario_redispatch_return(*, prefix: str, receipts_path: Path) -> dict[str, Any]:
    """Same LangGraph dispatch twice → one charge + signed receipt."""
    from mycelium import get_ledger, load_config_from_string, verify_receipt

    yaml_text = _demo_yaml(
        prefix=prefix,
        receipts_path=str(receipts_path),
        lease_ttl=30.0,
        poll_timeout=10.0,
    )
    config = load_config_from_string(yaml_text)
    calls: list[float] = []

    # One business id, two uses: Mycelium transition identity (request_id) and
    # the provider idempotency key. Mycelium never forwards request_id into the
    # tool body — pass the same string separately as ``idempotency_key`` so the
    # provider can dedupe too (``Idempotency-Key: charge-order:ORD-…``).
    request_id = f"charge-order:ORD-{uuid.uuid4().hex[:8]}"

    @config.apply
    def send_payment(
        amount: float,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> dict[str, float]:
        """Charge once (happy-path demo)."""
        calls.append(amount)
        return {"charged": amount, "provider_idempotency_key": idempotency_key}

    graph = _graph_for(send_payment)
    runtime = _runtime_config("thread-demo", "run-happy")
    call_id = f"call_happy_{uuid.uuid4().hex[:8]}"
    dispatch = dict(request_id=request_id, idempotency_key=request_id)

    first = graph.invoke(
        {"messages": [_tool_message(10.0, call_id, **dispatch)]}, runtime
    )
    second = graph.invoke(
        {"messages": [_tool_message(10.0, call_id, **dispatch)]}, runtime
    )

    ledger = get_ledger(send_payment)
    assert ledger is not None
    entries = ledger.list_transitions()
    assert len(entries) == 1, entries
    entry = entries[0]
    assert entry.receipt_ref, "expected signed receipt_ref on completed transition"
    assert calls == [10.0], f"body must run once, got {calls}"
    assert '"charged": 10.0' in first["messages"][-1].content
    assert '"charged": 10.0' in second["messages"][-1].content

    emitter = config.build_audit_receipt()
    assert emitter is not None
    receipts = emitter.storage.list_all()
    assert len(receipts) == 1
    assert verify_receipt(receipts[0], os.environ[ENV_SIGNING_KEY])

    return {
        "scenario": "redispatch_return",
        "executions": len(calls),
        "receipt_ref": entry.receipt_ref,
        "terminal_outcome": entry.terminal_outcome,
    }


def _crash_worker(payload: dict[str, Any]) -> None:
    """Claim via LangGraph, signal ready, hang until SIGKILL."""
    import redis

    from mycelium import load_config_from_string

    os.environ[ENV_SIGNING_KEY] = payload["signing_key"]
    client = redis.Redis.from_url(payload["url"], decode_responses=True)
    try:
        config = load_config_from_string(payload["yaml"])

        @config.apply
        def send_payment(
            amount: float,
            idempotency_key: str,
            request_id: str | None = None,
        ) -> dict[str, float]:
            """Charge once (crash-worker demo — hangs after claim)."""
            client.incr(payload["exec_key"])
            client.set(payload["ready_key"], "1")
            time.sleep(3600.0)
            return {"charged": amount}

        graph = _graph_for(send_payment)
        graph.invoke(
            {"messages": [_tool_message(10.0, payload["call_id"], **payload["dispatch"])]},
            _runtime_config(payload["thread_id"], payload["run_id"]),
        )
    except Exception as exc:  # noqa: BLE001 — surface to parent
        client.set(payload["error_key"], f"{type(exc).__name__}: {exc}")
    finally:
        try:
            client.close()
        except Exception:
            pass


def scenario_crash_hard_block(*, prefix: str, receipts_path: Path) -> dict[str, Any]:
    """Kill mid-flight worker; redispatch must HARD_BLOCK (no second charge)."""
    import redis
    from langchain_core.messages import ToolMessage

    from mycelium import (
        LedgerHardBlockError,
        get_ledger,
        load_config_from_string,
    )

    lease_ttl = 1.0
    # Ledger entries live under ``prefix``; keep coord keys outside that
    # namespace so RedisLedgerStorage.list_all never parses them as entries.
    ledger_prefix = f"{prefix}ledger:"
    coord_prefix = f"{prefix}coord:"
    yaml_text = _demo_yaml(
        prefix=ledger_prefix,
        receipts_path=str(receipts_path),
        lease_ttl=lease_ttl,
        poll_timeout=5.0,
    )
    run_token = uuid.uuid4().hex[:10]
    call_id = f"call_crash_{run_token}"
    ready_key = f"{coord_prefix}ready"
    exec_key = f"{coord_prefix}exec"
    error_key = f"{coord_prefix}error"
    url = _redis_url()

    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        client.delete(ready_key, exec_key, error_key)
    finally:
        client.close()

    payload = {
        "url": url,
        "yaml": yaml_text,
        "signing_key": os.environ[ENV_SIGNING_KEY],
        "call_id": call_id,
        "thread_id": "thread-crash",
        "run_id": "run-crash",
        "dispatch": dict(
            request_id=f"charge-order:ORD-{run_token}",
            idempotency_key=f"charge-order:ORD-{run_token}",
        ),
        "ready_key": ready_key,
        "exec_key": exec_key,
        "error_key": error_key,
    }

    ctx = mp.get_context("spawn")
    proc = ctx.Process(target=_crash_worker, args=(payload,), name="mycelium-crash-a")
    proc.start()
    try:
        client = redis.Redis.from_url(url, decode_responses=True)
        try:
            deadline = time.time() + 10.0
            while client.get(ready_key) != "1":
                if time.time() >= deadline:
                    err = client.get(error_key)
                    raise TimeoutError(
                        f"crash worker never claimed (error={err!r})"
                    )
                if not proc.is_alive():
                    raise RuntimeError(
                        f"crash worker exited early: {client.get(error_key)!r}"
                    )
                time.sleep(0.02)
        finally:
            client.close()

        proc.kill()
        proc.join(timeout=5.0)
        # Lease must lapse so redispatch sees EXPIRED, not POLL.
        time.sleep(lease_ttl + 0.5)
    finally:
        if proc.is_alive():
            proc.kill()
            proc.join(timeout=2.0)

    config = load_config_from_string(yaml_text)
    calls: list[float] = []

    @config.apply
    def send_payment(
        amount: float,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> dict[str, float]:
        """Charge once (post-crash redispatch — must not run)."""
        calls.append(amount)
        return {"charged": amount, "redispatch": True}

    graph = _graph_for(send_payment)
    hard_blocked = False
    try:
        result = graph.invoke(
            {"messages": [_tool_message(10.0, call_id, **payload["dispatch"])]},
            _runtime_config("thread-crash", "run-crash"),
        )
        # ToolNode may surface HARD_BLOCK as a ToolMessage error instead of raise.
        last = result["messages"][-1]
        if isinstance(last, ToolMessage) and "LedgerHardBlockError" in str(
            getattr(last, "content", "")
        ):
            hard_blocked = True
        else:
            raise AssertionError(
                f"expected HARD_BLOCK after crash, got tool result: {last!r}"
            )
    except LedgerHardBlockError:
        hard_blocked = True
    except Exception as exc:
        if _is_hard_block(exc):
            hard_blocked = True
        else:
            raise

    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        worker_execs = int(client.get(exec_key) or 0)
    finally:
        client.close()

    assert hard_blocked, "redispatch after crash must hard-block"
    assert worker_execs == 1, f"worker body once, got {worker_execs}"
    assert calls == [], f"redispatch must not re-run body, got {calls}"

    ledger = get_ledger(send_payment)
    assert ledger is not None
    assert ledger.list_transitions(), "expected durable crash transition in Redis"

    return {
        "scenario": "crash_hard_block",
        "executions": worker_execs,
        "redispatch_body_runs": len(calls),
        "hard_blocked": hard_blocked,
        "call_id": call_id,
    }


def _is_hard_block(exc: BaseException) -> bool:
    from mycelium import LedgerHardBlockError

    seen: set[int] = set()
    stack = [exc]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        if isinstance(cur, LedgerHardBlockError):
            return True
        if "LedgerHardBlockError" in type(cur).__name__:
            return True
        cause = getattr(cur, "__cause__", None)
        ctx = getattr(cur, "__context__", None)
        if cause is not None:
            stack.append(cause)
        if ctx is not None:
            stack.append(ctx)
    return False


def main() -> int:
    try:
        import langgraph  # noqa: F401
        import redis  # noqa: F401
    except ImportError as exc:
        print(
            "missing dependency:",
            exc,
            "\ninstall: pip install 'mycelium-runtime[langgraph]' redis",
            file=sys.stderr,
        )
        return 2

    os.environ.setdefault(ENV_REDIS_URL, DEFAULT_REDIS_URL)
    os.environ.setdefault(ENV_SIGNING_KEY, "demo-signing-key")
    url = _redis_url()
    if not redis_reachable(url):
        print(
            f"Redis not reachable at {url!r}.\n"
            f"  docker run -d --name mycelium-redis -p 6379:6379 redis:7\n"
            f"  export {ENV_REDIS_URL}={DEFAULT_REDIS_URL}",
            file=sys.stderr,
        )
        return 2

    run_id = uuid.uuid4().hex
    prefix = f"mycelium:example:lg_crash:{run_id}:"
    tmp = Path(tempfile.mkdtemp(prefix="mycelium-lg-crash-"))
    receipts = tmp / "receipts.jsonl"

    print("Mycelium adoption example: LangGraph + Redis + receipts + crash")
    print(f"  redis={url}")
    print(f"  prefix={prefix}")
    print()

    try:
        happy = scenario_redispatch_return(prefix=prefix + "happy:", receipts_path=receipts)
        print(
            f"[ok] redispatch RETURN — executions={happy['executions']} "
            f"receipt_ref={happy['receipt_ref']}"
        )

        crash = scenario_crash_hard_block(
            prefix=prefix + "crash:",
            receipts_path=tmp / "crash-receipts.jsonl",
        )
        print(
            f"[ok] crash HARD_BLOCK — worker_executions={crash['executions']} "
            f"redispatch_body_runs={crash['redispatch_body_runs']}"
        )
    finally:
        _cleanup_prefix(url, prefix)

    print()
    print("Proof: same tool_call never double-charges across redispatch or crash.")
    print("Copy mycelium.example.yaml -> mycelium.yaml; keep Redis + signing_key in prod.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
