"""Black-box tests for ``mycelium run``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _run(
    tmp_path: Path,
    config: Path,
    module: str,
    *args: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(tmp_path)
        if not existing_pythonpath
        else os.pathsep.join((str(tmp_path), existing_pythonpath))
    )
    env.update(extra_env or {})
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mycelium",
            "run",
            "--config",
            str(config),
            "--",
            sys.executable,
            "-m",
            module,
            *args,
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_run_instruments_sync_tool_and_task_across_processes(
    tmp_path: Path,
) -> None:
    effect_file = tmp_path / "effects.txt"
    tool_ledger = tmp_path / "tool-ledger.json"
    task_ledger = tmp_path / "task-ledger.json"
    config = tmp_path / "mycelium.yaml"

    _write(
        tmp_path / "runtime_targets.py",
        """
import os
from pathlib import Path

effect_file = Path(os.environ["EFFECT_FILE"])

def charge(amount: float) -> dict:
    with effect_file.open("a", encoding="utf-8") as handle:
        handle.write(f"charge:{amount}\\n")
    return {"amount": amount}

def invoice(invoice_id: str) -> dict:
    with effect_file.open("a", encoding="utf-8") as handle:
        handle.write(f"invoice:{invoice_id}\\n")
    return {"invoice_id": invoice_id}
""",
    )
    _write(
        tmp_path / "runtime_app.py",
        """
import os
import sys
from pathlib import Path
from runtime_targets import charge, invoice

print(f"argv={sys.argv[1:]}")
print(f"cwd={Path.cwd()}")
print(f"marker={os.environ.get('MYCELIUM_AUTO_INSTRUMENT')}")
print(charge(amount=5.0, request_id="payment-1"))
print(charge(amount=5.0, request_id="payment-1"))
print(invoice(invoice_id="inv-1"))
print(invoice(invoice_id="inv-1"))
""",
    )
    _write(
        config,
        f"""
action_ledger:
  storage: file
  path: {tool_ledger}
  tools: [send_payment]
task_ledger:
  storage: file
  path: {task_ledger}
  tasks: [process_invoice]
tools:
  send_payment:
    callable: runtime_targets:charge
tasks:
  process_invoice:
    callable: runtime_targets:invoice
    id_from: [invoice_id]
""",
    )

    env = {"EFFECT_FILE": str(effect_file)}
    first = _run(tmp_path, config, "runtime_app", "hello world", extra_env=env)
    second = _run(tmp_path, config, "runtime_app", "hello world", extra_env=env)

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert "argv=['hello world']" in first.stdout
    assert f"cwd={tmp_path}" in first.stdout
    assert "marker=None" in first.stdout
    assert effect_file.read_text(encoding="utf-8").splitlines() == [
        "charge:5.0",
        "invoice:inv-1",
    ]


def test_run_instruments_async_tool(tmp_path: Path) -> None:
    effect_file = tmp_path / "async-effects.txt"
    config = tmp_path / "mycelium.yaml"
    _write(
        tmp_path / "async_targets.py",
        """
import os
from pathlib import Path

async def send(to: str) -> str:
    with Path(os.environ["EFFECT_FILE"]).open("a", encoding="utf-8") as handle:
        handle.write(to + "\\n")
    return to
""",
    )
    _write(
        tmp_path / "async_app.py",
        """
import asyncio
from async_targets import send

async def main():
    print(await send(to="a@example.com", request_id="email-1"))
    print(await send(to="a@example.com", request_id="email-1"))

asyncio.run(main())
""",
    )
    _write(
        config,
        f"""
action_ledger:
  storage: file
  path: {tmp_path / "async-ledger.json"}
  tools: [send_email]
tools:
  send_email:
    callable: async_targets:send
""",
    )

    result = _run(
        tmp_path,
        config,
        "async_app",
        extra_env={"EFFECT_FILE": str(effect_file)},
    )

    assert result.returncode == 0, result.stderr
    assert effect_file.read_text(encoding="utf-8").splitlines() == [
        "a@example.com"
    ]


def test_run_preserves_child_exit_code(tmp_path: Path) -> None:
    config = tmp_path / "mycelium.yaml"
    _write(tmp_path / "exit_targets.py", "def tool(): return 'ok'\n")
    _write(tmp_path / "exit_app.py", "raise SystemExit(7)\n")
    _write(
        config,
        """
action_ledger: {storage: memory, tools: [tool]}
tools:
  tool:
    callable: exit_targets:tool
""",
    )

    result = _run(tmp_path, config, "exit_app")
    assert result.returncode == 7


def test_run_fails_closed_for_missing_target(tmp_path: Path) -> None:
    config = tmp_path / "mycelium.yaml"
    _write(tmp_path / "missing_targets.py", "value = 1\n")
    _write(tmp_path / "should_not_run.py", "print('APP_RAN')\n")
    _write(
        config,
        """
action_ledger: {storage: memory, tools: [missing]}
tools:
  missing:
    callable: missing_targets:not_here
""",
    )

    result = _run(tmp_path, config, "should_not_run")

    assert result.returncode == 78
    assert "auto-instrumentation failed" in result.stderr
    assert "does not exist" in result.stderr
    assert "APP_RAN" not in result.stdout


def test_run_propagates_langgraph_tool_runtime_ids(tmp_path: Path) -> None:
    effect_file = tmp_path / "langgraph-effects.txt"
    config = tmp_path / "mycelium.yaml"
    _write(
        tmp_path / "langgraph_targets.py",
        """
import os
from pathlib import Path

def charge(amount: float) -> dict:
    \"\"\"Charge a payment once.\"\"\"
    with Path(os.environ["EFFECT_FILE"]).open("a", encoding="utf-8") as handle:
        handle.write(str(amount) + "\\n")
    return {"amount": amount}
""",
    )
    _write(
        tmp_path / "langgraph_app.py",
        """
from langchain_core.messages import AIMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph_targets import charge

builder = StateGraph(MessagesState)
builder.add_node("tools", ToolNode([charge]))
builder.add_edge(START, "tools")
builder.add_edge("tools", END)
graph = builder.compile()

for _ in range(2):
    message = AIMessage(
        content="",
        tool_calls=[{
            "name": "send_payment",
            "args": {"amount": 9.0},
            "id": "call-1",
            "type": "tool_call",
        }],
    )
    graph.invoke(
        {"messages": [message]},
        {"configurable": {"thread_id": "thread-1"}, "run_id": "run-1"},
    )
""",
    )
    _write(
        config,
        f"""
integrations: {{langgraph: {{enabled: true}}}}
transition: {{agent_id: test, policy_version: "1"}}
action_ledger:
  storage: file
  path: {tmp_path / "langgraph-ledger.json"}
  tools: [send_payment]
tools:
  send_payment:
    callable: langgraph_targets:charge
    side_effect_class: non_idempotent_mutate
""",
    )

    result = _run(
        tmp_path,
        config,
        "langgraph_app",
        extra_env={"EFFECT_FILE": str(effect_file)},
    )

    assert result.returncode == 0, result.stderr
    assert effect_file.read_text(encoding="utf-8").splitlines() == ["9.0"]
