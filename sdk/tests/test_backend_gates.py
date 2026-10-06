"""Required backend gates must fail rather than silently skip a missing driver."""

import pytest
from backend_gates import require_postgres_dsn_or_skip, require_redis_or_skip


@pytest.mark.parametrize(
    ("gate", "required_env"),
    [
        (require_redis_or_skip, "MYCELIUM_CI_REQUIRE_REDIS"),
        (require_postgres_dsn_or_skip, "MYCELIUM_CI_REQUIRE_POSTGRES"),
    ],
)
@pytest.mark.parametrize("required", [False, True])
def test_missing_driver_obeys_required_backend_policy(monkeypatch, gate, required_env, required):
    def missing_driver(module):
        pytest.skip(f"no {module} driver")

    monkeypatch.setattr(pytest, "importorskip", missing_driver)
    monkeypatch.setenv(required_env, "1" if required else "0")
    exception = pytest.fail.Exception if required else pytest.skip.Exception
    with pytest.raises(exception):
        gate()
