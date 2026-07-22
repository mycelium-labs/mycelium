"""Real Redis two-worker Cloud-style #7417 redispatch proof."""

from __future__ import annotations

import pytest

from mycelium.proofs.langgraph_7417_redis import (
    ENV_REDIS_URL,
    prove_two_worker_redis_redispatch,
    redis_reachable,
    resolve_redis_url,
)

pytest.importorskip("redis")

_REDIS_URL = resolve_redis_url()
pytestmark = pytest.mark.skipif(
    not redis_reachable(_REDIS_URL),
    reason=(
        f"real Redis required at {_REDIS_URL!r} "
        f"(set {ENV_REDIS_URL} or start redis-server)"
    ),
)


def test_two_worker_redis_cloud_style_redispatch() -> None:
    result = prove_two_worker_redis_redispatch()
    assert result["workers"] == 2
    assert result["storage"] == "redis"
    assert result["executions"] == 1
    assert result["result"] == {"task": "analyze_market", "result": "done"}
