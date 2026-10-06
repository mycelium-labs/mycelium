"""Shared helpers for Redis/Postgres integration gates."""

from __future__ import annotations

import os

import pytest

from mycelium.proofs.langgraph_7417_redis import (
    ENV_REDIS_URL,
    redis_reachable,
    resolve_redis_url,
)

ENV_REQUIRE_REDIS = "MYCELIUM_CI_REQUIRE_REDIS"
ENV_REQUIRE_POSTGRES = "MYCELIUM_CI_REQUIRE_POSTGRES"
ENV_POSTGRES_DSN = "MYCELIUM_TEST_POSTGRES_DSN"


def _require_driver_or_skip(module: str, required_env: str) -> None:
    try:
        pytest.importorskip(module)
    except pytest.skip.Exception:
        if os.environ.get(required_env) == "1":
            pytest.fail(f"{module} driver is required by {required_env}=1", pytrace=False)
        raise


def require_redis_or_skip() -> str:
    """Return a reachable Redis URL, or skip/fail depending on CI policy.

    Locally: skip when Redis is missing (dev machines without docker).
    In CI (``MYCELIUM_CI_REQUIRE_REDIS=1``): fail hard so a silent skip
    cannot greenwash the concurrency proofs.
    """
    _require_driver_or_skip("redis", ENV_REQUIRE_REDIS)
    url = resolve_redis_url()
    if redis_reachable(url):
        return url
    msg = f"real Redis required at {url!r} (set {ENV_REDIS_URL} or start redis-server)"
    if os.environ.get(ENV_REQUIRE_REDIS) == "1":
        pytest.fail(msg)
    pytest.skip(msg)


def require_postgres_dsn_or_skip() -> str:
    """Return Postgres DSN from env, or skip/fail depending on CI policy."""
    _require_driver_or_skip("psycopg", ENV_REQUIRE_POSTGRES)
    dsn = os.environ.get(ENV_POSTGRES_DSN)
    if dsn:
        return dsn
    msg = f"set {ENV_POSTGRES_DSN} to run Postgres integration tests"
    if os.environ.get(ENV_REQUIRE_POSTGRES) == "1":
        pytest.fail(msg)
    pytest.skip(msg)
