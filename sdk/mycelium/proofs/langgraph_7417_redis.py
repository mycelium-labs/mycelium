"""Cloud-style #7417 proof: two workers + real Redis shared ledger.

Worker A claims and runs a slow side effect. Worker B redispatches the same
``request_id`` while A is still ``IN_FLIGHT``. With a shared Redis ledger, B
must poll and return A's result — the side effect runs once.

Requires a reachable Redis (``MYCELIUM_TEST_REDIS_URL`` or localhost).
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
import uuid
from typing import Any

DEFAULT_REDIS_URL = "redis://127.0.0.1:6379/15"
ENV_REDIS_URL = "MYCELIUM_TEST_REDIS_URL"


def resolve_redis_url() -> str:
    """Return Redis URL from env or the dedicated local proof DB."""
    return os.environ.get(ENV_REDIS_URL) or DEFAULT_REDIS_URL


def redis_reachable(url: str | None = None) -> bool:
    """True when ``redis`` is installed and ``PING`` succeeds."""
    try:
        import redis
    except ImportError:
        return False
    client = redis.Redis.from_url(url or resolve_redis_url(), decode_responses=True)
    try:
        return bool(client.ping())
    except Exception:
        return False
    finally:
        try:
            client.close()
        except Exception:
            pass


def _coord_keys(run_id: str) -> dict[str, str]:
    base = f"mycelium:proof:7417:{run_id}"
    return {
        "prefix": f"{base}:ledger:",
        "exec": f"{base}:executions",
        "ready": f"{base}:ready",
        "error_a": f"{base}:error:a",
        "error_b": f"{base}:error:b",
        "result_b": f"{base}:result:b",
    }


def _cleanup(url: str, keys: dict[str, str]) -> None:
    import redis

    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        to_delete = [
            keys["exec"],
            keys["ready"],
            keys["error_a"],
            keys["error_b"],
            keys["result_b"],
        ]
        for key in client.scan_iter(match=f"{keys['prefix']}*"):
            to_delete.append(key)
        if to_delete:
            client.delete(*to_delete)
    finally:
        client.close()


def _worker_a(payload: dict[str, Any]) -> None:
    """Claim, signal ready, run side effect, complete."""
    import redis

    from mycelium import (
        ActionLedger,
        RedisLedgerStorage,
        SideEffectClass,
        ToolTransitionBinding,
    )

    url = payload["url"]
    keys = payload["keys"]
    request_id = payload["request_id"]
    work_seconds = float(payload["work_seconds"])
    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        storage = RedisLedgerStorage(url, prefix=keys["prefix"], in_flight_ttl=3600.0)
        ledger = ActionLedger(
            storage=storage,
            lease_ttl=float(payload["lease_ttl"]),
            poll_interval=0.05,
            poll_timeout=float(payload["poll_timeout"]),
        )
        binding = ToolTransitionBinding.for_tool(
            agent_id="proof-agent",
            policy_version="1",
            side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
        )
        ledger.claim_side_effecting(
            request_id,
            "subagent_task",
            (),
            {"task": "analyze_market"},
            binding,
        )
        client.set(keys["ready"], "1")
        # Durable side-effect counter shared across processes.
        client.incr(keys["exec"])
        time.sleep(work_seconds)
        ledger.complete(request_id, {"task": "analyze_market", "result": "done"})
    except Exception as exc:  # noqa: BLE001 — surface to parent via Redis
        client.set(keys["error_a"], f"{type(exc).__name__}: {exc}")
    finally:
        client.close()


def _worker_b(payload: dict[str, Any]) -> None:
    """Wait for A's claim, then redispatch the same transition key."""
    import json

    import redis

    from mycelium import (
        ActionLedger,
        RedisLedgerStorage,
        SideEffectClass,
        ToolTransitionBinding,
    )

    url = payload["url"]
    keys = payload["keys"]
    request_id = payload["request_id"]
    client = redis.Redis.from_url(url, decode_responses=True)
    try:
        deadline = time.time() + float(payload["ready_timeout"])
        while client.get(keys["ready"]) != "1":
            if time.time() >= deadline:
                raise TimeoutError("worker A never signaled ready")
            time.sleep(0.02)

        storage = RedisLedgerStorage(url, prefix=keys["prefix"], in_flight_ttl=3600.0)
        ledger = ActionLedger(
            storage=storage,
            lease_ttl=float(payload["lease_ttl"]),
            poll_interval=0.05,
            poll_timeout=float(payload["poll_timeout"]),
        )
        binding = ToolTransitionBinding.for_tool(
            agent_id="proof-agent",
            policy_version="1",
            side_effect_class=SideEffectClass.NON_IDEMPOTENT_MUTATE,
        )
        entry = ledger.claim_side_effecting(
            request_id,
            "subagent_task",
            (),
            {"task": "analyze_market"},
            binding,
        )
        client.set(
            keys["result_b"],
            json.dumps(
                {
                    "result": entry.result,
                    "terminal_outcome": entry.terminal_outcome,
                    "executions": int(client.get(keys["exec"]) or 0),
                }
            ),
        )
    except Exception as exc:  # noqa: BLE001 — surface to parent via Redis
        client.set(keys["error_b"], f"{type(exc).__name__}: {exc}")
    finally:
        client.close()


def prove_two_worker_redis_redispatch(
    *,
    url: str | None = None,
    work_seconds: float = 0.4,
    lease_ttl: float = 30.0,
    poll_timeout: float = 10.0,
    ready_timeout: float = 5.0,
) -> dict[str, Any]:
    """Run the two-worker Redis proof. Raises ``AssertionError`` on failure.

    Spawns two OS processes that share a Redis ledger. Worker B starts after
    worker A has claimed ``IN_FLIGHT`` and redispatches the same ``request_id``.
    """
    import json

    import redis

    url = url or resolve_redis_url()
    if not redis_reachable(url):
        raise RuntimeError(
            f"Redis not reachable at {url!r}; set {ENV_REDIS_URL} or start Redis"
        )

    run_id = uuid.uuid4().hex
    request_id = f"call_subagent_redis_{run_id}"
    keys = _coord_keys(run_id)
    payload = {
        "url": url,
        "keys": keys,
        "request_id": request_id,
        "work_seconds": work_seconds,
        "lease_ttl": lease_ttl,
        "poll_timeout": poll_timeout,
        "ready_timeout": ready_timeout,
    }

    _cleanup(url, keys)
    ctx = mp.get_context("spawn")
    proc_a = ctx.Process(target=_worker_a, args=(payload,), name="mycelium-proof-a")
    proc_b = ctx.Process(target=_worker_b, args=(payload,), name="mycelium-proof-b")
    try:
        proc_a.start()
        proc_b.start()
        proc_a.join(timeout=poll_timeout + work_seconds + 5.0)
        proc_b.join(timeout=poll_timeout + work_seconds + 5.0)
        if proc_a.is_alive() or proc_b.is_alive():
            proc_a.terminate()
            proc_b.terminate()
            proc_a.join(timeout=2.0)
            proc_b.join(timeout=2.0)
            raise AssertionError("worker process timed out")

        client = redis.Redis.from_url(url, decode_responses=True)
        try:
            err_a = client.get(keys["error_a"])
            err_b = client.get(keys["error_b"])
            if err_a:
                raise AssertionError(f"worker A failed: {err_a}")
            if err_b:
                raise AssertionError(f"worker B failed: {err_b}")
            raw = client.get(keys["result_b"])
            if not raw:
                raise AssertionError("worker B did not publish a result")
            body = json.loads(raw)
            executions = int(client.get(keys["exec"]) or 0)
        finally:
            client.close()

        assert executions == 1, f"expected 1 side effect, got {executions}"
        assert body["executions"] == 1
        assert body["result"] == {"task": "analyze_market", "result": "done"}
        assert body["terminal_outcome"] == "COMPLETED"
        assert proc_a.exitcode == 0, f"worker A exit {proc_a.exitcode}"
        assert proc_b.exitcode == 0, f"worker B exit {proc_b.exitcode}"

        return {
            "url": url,
            "request_id": request_id,
            "executions": executions,
            "result": body["result"],
            "workers": 2,
            "storage": "redis",
        }
    finally:
        _cleanup(url, keys)


__all__ = [
    "DEFAULT_REDIS_URL",
    "ENV_REDIS_URL",
    "prove_two_worker_redis_redispatch",
    "redis_reachable",
    "resolve_redis_url",
]
