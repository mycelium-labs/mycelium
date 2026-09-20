"""Smoke coverage for the bundled ``sdk/examples/`` (#189).

The manifest below is the curated list of examples that CI keeps working. It is
documented in ``sdk/examples/README.md`` and checked against the directory so a
new example cannot land without being classified.

Every example is in exactly one mode:

``script``    Run as ``python <example>`` in a subprocess; exit code 0 and a
              behavioural marker on stdout are required.
``config``    A YAML example that must satisfy the current configuration schema
              and load through ``load_config()``.
``stubbed``   Imported and driven in-process with stubbed provider and model
              callables, so no credential or network is involved.
``existing``  Already exercised by a dedicated test module (named in the
              manifest); the smoke suite only checks that the module is still
              there and still references the example.

Examples that need credentials or a network are never called live. Their live
path is listed in ``SKIPPED_LIVE`` and reported through an explicit
``pytest.skip`` reason (see ``pytest -rs``).

Scripts run with a hermetic environment: provider credentials and
``MYCELIUM_*`` backend settings are removed and every non-loopback socket
connection raises, so an example that starts making external calls fails here
instead of in a user's terminal. stdout is pinned to ``cp1252`` (the default
Windows console encoding) because examples that print other characters crash on
Windows.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest
import yaml

from mycelium import load_config
from mycelium.config_schema import validate_config_shape

SDK_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent
EXAMPLES_DIR = SDK_ROOT / "examples"
EXAMPLES_README = EXAMPLES_DIR / "README.md"

SCRIPT_TIMEOUT_SECONDS = 120

# Categories every smoke run must cover with at least one non-skipped example.
CATEGORIES = (
    "langgraph",
    "failure-gates",
    "python-guards",
    "webhooks",
    "agent-app",
    "config",
)


@dataclass(frozen=True)
class Example:
    path: str  # relative to sdk/examples, POSIX separators
    category: str
    mode: str  # script | config | stubbed | existing
    marker: str = ""  # script: text required on stdout
    covered_by: str = ""  # existing: test module under sdk/tests


MANIFEST: tuple[Example, ...] = (
    # LangGraph-shaped workflow using the normal wrapper, local sandbox provider.
    Example(
        "langgraph_email_onboarding/run.py",
        "langgraph",
        "script",
        marker="sandbox provider calls: 1",
    ),
    Example("langgraph_email_onboarding/mycelium.yaml", "langgraph", "config"),
    # Real LangGraph ToolNode + Redis + crash; needs Redis, so it keeps its own test.
    Example(
        "langgraph_redis_crash/run.py",
        "langgraph",
        "existing",
        covered_by="test_example_langgraph_redis_crash.py",
    ),
    Example("langgraph_redis_crash/mycelium.example.yaml", "langgraph", "config"),
    # In-process gate repros (RETURN / POLL / HARD_BLOCK / REPAIR / reconcile).
    Example(
        "failure_cases/run_all.py",
        "failure-gates",
        "existing",
        covered_by="test_failure_cases.py",
    ),
    Example(
        "failure_cases/01_return_completed.py",
        "failure-gates",
        "existing",
        covered_by="test_failure_cases.py",
    ),
    Example(
        "failure_cases/02_poll_in_flight.py",
        "failure-gates",
        "existing",
        covered_by="test_failure_cases.py",
    ),
    Example(
        "failure_cases/03_hard_block_unknown.py",
        "failure-gates",
        "existing",
        covered_by="test_failure_cases.py",
    ),
    Example(
        "failure_cases/04_repair_incomplete.py",
        "failure-gates",
        "existing",
        covered_by="test_failure_cases.py",
    ),
    Example(
        "failure_cases/05_reconcile_completed.py",
        "failure-gates",
        "existing",
        covered_by="test_failure_cases.py",
    ),
    # Optional run-level guards used from plain Python.
    Example(
        "loop_guard_db_search.py",
        "python-guards",
        "script",
        marker="allow-once ran:",
    ),
    Example(
        "scope_guard_allowlist.py",
        "python-guards",
        "script",
        marker="violation=scope_escalation_tool",
    ),
    Example(
        "completion_contract_checklist.py",
        "python-guards",
        "script",
        marker="verdict=allow_with_warnings",
    ),
    # Webhook event-id dedupe recipes (fakes only, no provider credentials).
    Example(
        "webhooks/stripe_handler.py",
        "webhooks",
        "script",
        marker="work ran for: ['evt_1', 'evt_2']",
    ),
    Example(
        "webhooks/github_handler.py",
        "webhooks",
        "script",
        marker="work ran for: ['deliv_1']",
    ),
    Example(
        "webhooks/twilio_handler.py",
        "webhooks",
        "script",
        marker="work ran for: ['SM1001', 'SM1002']",
    ),
    # Real agent application; the Groq decision call is stubbed here.
    Example("groq_support_agent.py", "agent-app", "stubbed"),
    # Generated reference configuration.
    Example("mycelium.generated.example.yaml", "config", "config"),
)

# Live paths that are never exercised in CI. (label, reason)
SKIPPED_LIVE: tuple[tuple[str, str], ...] = (
    (
        "groq_support_agent.py: GroqDecisionModel live call",
        "requires GROQ_API_KEY and network access to api.groq.com; the agent is "
        "smoke-tested with a stub decision model instead",
    ),
)

_BY_MODE = {
    mode: tuple(e for e in MANIFEST if e.mode == mode)
    for mode in ("script", "config", "stubbed", "existing")
}

_NETWORK_GUARD = textwrap.dedent(
    '''\
    """Test-only: refuse non-loopback socket connections."""
    import ipaddress
    import socket

    _connect = socket.socket.connect
    _connect_ex = socket.socket.connect_ex


    def _check(sock, address):
        if getattr(socket, "AF_UNIX", None) is not None and sock.family == socket.AF_UNIX:
            return
        host = address[0] if isinstance(address, tuple) else address
        if isinstance(host, str):
            if host == "localhost":
                return
            try:
                if ipaddress.ip_address(host).is_loopback:
                    return
            except ValueError:
                pass
        raise OSError(f"examples must not make external network calls: {address!r}")


    def connect(self, address):
        _check(self, address)
        return _connect(self, address)


    def connect_ex(self, address):
        _check(self, address)
        return _connect_ex(self, address)


    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    '''
)

_DROP_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_DSN")


def _hermetic_env(tmp_path: Path) -> dict[str, str]:
    guard_dir = tmp_path / "network_guard"
    guard_dir.mkdir(exist_ok=True)
    (guard_dir / "sitecustomize.py").write_text(_NETWORK_GUARD, encoding="utf-8")

    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("MYCELIUM_") and not key.endswith(_DROP_SUFFIXES)
    }
    env.pop("PYTHONUTF8", None)
    env["PYTHONIOENCODING"] = "cp1252"
    env["PYTHONPATH"] = os.pathsep.join([str(guard_dir), str(SDK_ROOT)])
    return env


def _run_script(relative: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed interpreter, repo-owned script path
        [sys.executable, str(EXAMPLES_DIR / relative)],
        cwd=str(tmp_path),
        env=_hermetic_env(tmp_path),
        capture_output=True,
        text=True,
        encoding="cp1252",
        errors="replace",
        timeout=SCRIPT_TIMEOUT_SECONDS,
        check=False,
    )


def _ids(examples: tuple[Example, ...]) -> list[str]:
    return [e.path for e in examples]


# --- manifest integrity ----------------------------------------------------


def _discovered_examples() -> set[str]:
    found: set[str] = set()
    for pattern in ("*.py", "*.yaml", "*.yml"):
        for path in EXAMPLES_DIR.rglob(pattern):
            relative = path.relative_to(EXAMPLES_DIR)
            if "__pycache__" in relative.parts or relative.name.startswith("_"):
                continue  # ``_narrate.py``-style private helpers are not examples
            found.add(relative.as_posix())
    return found


def test_manifest_lists_every_bundled_example() -> None:
    """A new example must be classified here (run it, or say why it is skipped)."""
    listed = {e.path for e in MANIFEST}
    discovered = _discovered_examples()
    assert not discovered - listed, (
        "examples not classified in tests/test_examples_smoke.py MANIFEST: "
        f"{sorted(discovered - listed)}"
    )
    assert not listed - discovered, (
        f"MANIFEST entries with no file under sdk/examples/: {sorted(listed - discovered)}"
    )
    assert len(listed) == len(MANIFEST), "duplicate MANIFEST paths"


def test_every_supported_category_is_covered() -> None:
    covered = {e.category for e in MANIFEST}
    assert set(CATEGORIES) <= covered, f"uncovered categories: {set(CATEGORIES) - covered}"
    assert covered <= set(CATEGORIES), f"unknown categories: {covered - set(CATEGORIES)}"


def test_curated_list_is_documented() -> None:
    assert EXAMPLES_README.is_file(), "sdk/examples/README.md documents the curated list"
    text = EXAMPLES_README.read_text(encoding="utf-8")
    missing = [e.path for e in MANIFEST if e.path not in text]
    assert not missing, f"examples missing from sdk/examples/README.md: {missing}"
    for _, reason in SKIPPED_LIVE:
        assert reason.split(";")[0] in text, "skip reason must be documented"


# --- script examples ---------------------------------------------------------


@pytest.mark.parametrize("example", _BY_MODE["script"], ids=_ids(_BY_MODE["script"]))
def test_script_example_runs_offline(example: Example, tmp_path: Path) -> None:
    completed = _run_script(example.path, tmp_path)
    output = completed.stdout + "\n" + completed.stderr
    assert completed.returncode == 0, f"{example.path} exited {completed.returncode}:\n{output}"
    assert example.marker in completed.stdout, (
        f"{example.path}: expected {example.marker!r} on stdout:\n{output}"
    )


def test_network_guard_blocks_external_connections(tmp_path: Path) -> None:
    """Prove the hermetic environment would catch an external call."""
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import socket; socket.create_connection(('203.0.113.1', 80), timeout=1)",
        ],
        cwd=str(tmp_path),
        env=_hermetic_env(tmp_path),
        capture_output=True,
        text=True,
        timeout=SCRIPT_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode != 0
    assert "must not make external network calls" in completed.stderr


# --- config examples ---------------------------------------------------------


@pytest.mark.parametrize("example", _BY_MODE["config"], ids=_ids(_BY_MODE["config"]))
def test_config_example_matches_current_schema(example: Example) -> None:
    path = EXAMPLES_DIR / example.path
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{example.path} is not a YAML mapping"
    validate_config_shape(data)  # raises when a field drifts from the typed model
    assert load_config(path) is not None


# --- stubbed examples --------------------------------------------------------


def _load_example_module(relative: str, name: str) -> ModuleType:
    path = EXAMPLES_DIR / relative
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_groq_support_agent_with_stubbed_model_dedupes_refunds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_example_module("groq_support_agent.py", "groq_support_agent_example")

    def _no_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("groq_support_agent must not call the network in CI")

    monkeypatch.setattr(module, "urlopen", _no_network)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)

    refunds: list[tuple[str, str, str]] = []

    def refund_provider(order_id, amount, idempotency_key):  # type: ignore[no-untyped-def]
        refunds.append((order_id, str(amount), idempotency_key))
        return {"refund_id": f"re_{len(refunds)}", "order_id": order_id}

    decisions = {
        "refund": {"action": "refund", "order_id": "ORD-1", "amount": "10.00", "message": "ok"},
        "reply": {"action": "reply", "message": "Working on it"},
    }
    chosen = {"value": "refund"}

    def decision_model(_ticket, _message, _orders):  # type: ignore[no-untyped-def]
        return decisions[chosen["value"]]

    agent = module.CustomerSupportAgent(
        refund_provider,
        state_dir=tmp_path / "agent-state",
        decision_model=decision_model,
    )
    orders = {"ORD-1": "25.00"}

    first = agent.handle(ticket_id="T-1", customer_message="refund please", orders=orders)
    again = agent.handle(ticket_id="T-1", customer_message="refund please", orders=orders)
    assert first["action"] == "refund"
    assert again["refund"] == first["refund"], "retry must return the stored result"
    assert len(refunds) == 1, "the provider must run once per ticket/order"
    assert refunds[0][2] == "support-refund:T-1:ORD-1"

    chosen["value"] = "reply"
    reply = agent.handle(ticket_id="T-2", customer_message="hello", orders=orders)
    assert reply == {"action": "reply", "message": "Working on it"}
    assert len(refunds) == 1


def test_groq_decision_model_requires_credentials_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_example_module("groq_support_agent.py", "groq_support_agent_example")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(module.AgentError, match="GROQ_API_KEY"):
        module.GroqDecisionModel()


# --- examples covered by dedicated tests ------------------------------------


@pytest.mark.parametrize("example", _BY_MODE["existing"], ids=_ids(_BY_MODE["existing"]))
def test_existing_example_test_is_still_wired(example: Example) -> None:
    test_file = TESTS_DIR / example.covered_by
    assert test_file.is_file(), f"{example.path}: missing covering test {example.covered_by}"
    example_dir = Path(example.path).parts[0]
    assert example_dir in test_file.read_text(encoding="utf-8"), (
        f"{example.covered_by} no longer references examples/{example_dir}"
    )


# --- reported skips ----------------------------------------------------------


@pytest.mark.parametrize(("label", "reason"), SKIPPED_LIVE, ids=[s[0] for s in SKIPPED_LIVE])
def test_live_example_paths_are_skipped_with_reason(label: str, reason: str) -> None:
    pytest.skip(f"{label}: {reason}")
