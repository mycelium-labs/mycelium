"""Unit tests for command-based callable instrumentation."""

from __future__ import annotations

import asyncio
import inspect
import sys
from types import ModuleType

import pytest

from mycelium import ConfigError, ledger_sync, load_config_from_string
from mycelium.auto_instrumentation import instrument_configured_callables


def _module(monkeypatch, name: str, **attributes):
    module = ModuleType(name)
    for attribute, value in attributes.items():
        value.__module__ = name
        setattr(module, attribute, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_instruments_sync_tool_and_replaces_module_attribute(monkeypatch) -> None:
    calls: list[float] = []

    def charge(amount: float) -> dict[str, float]:
        calls.append(amount)
        return {"amount": amount}

    module = _module(monkeypatch, "auto_sync_tools", charge=charge)
    config = load_config_from_string(
        """
action_ledger: {storage: memory, tools: [send_payment]}
tools:
  send_payment:
    callable: auto_sync_tools:charge
"""
    )

    instrument_configured_callables(config)

    assert module.charge.__name__ == "send_payment"
    first = module.charge(amount=3.0, request_id="same")
    second = module.charge(amount=3.0, request_id="same")
    assert first == second == {"amount": 3.0}
    assert calls == [3.0]


def test_instruments_async_tool(monkeypatch) -> None:
    calls: list[str] = []

    async def send(to: str) -> str:
        calls.append(to)
        return to

    module = _module(monkeypatch, "auto_async_tools", send=send)
    config = load_config_from_string(
        """
action_ledger: {storage: memory, tools: [send_email]}
tools:
  send_email:
    callable: auto_async_tools:send
"""
    )

    instrument_configured_callables(config)

    assert inspect.iscoroutinefunction(module.send)

    async def run() -> None:
        assert await module.send(to="a@example.com", request_id="same") == "a@example.com"
        assert await module.send(to="a@example.com", request_id="same") == "a@example.com"

    asyncio.run(run())
    assert calls == ["a@example.com"]


def test_instruments_task(monkeypatch) -> None:
    calls: list[str] = []

    def run_invoice(invoice_id: str) -> dict[str, str]:
        calls.append(invoice_id)
        return {"invoice_id": invoice_id}

    module = _module(monkeypatch, "auto_tasks", run_invoice=run_invoice)
    config = load_config_from_string(
        """
task_ledger: {storage: memory, tasks: [process_invoice]}
tasks:
  process_invoice:
    callable: auto_tasks:run_invoice
    id_from: [invoice_id]
"""
    )

    instrument_configured_callables(config)

    assert module.run_invoice(invoice_id="inv-1") == {"invoice_id": "inv-1"}
    assert module.run_invoice(invoice_id="inv-1") == {"invoice_id": "inv-1"}
    assert calls == ["inv-1"]


def test_explicit_config_apply_is_not_wrapped_twice(monkeypatch) -> None:
    calls: list[float] = []

    def charge(amount: float) -> float:
        calls.append(amount)
        return amount

    config = load_config_from_string(
        """
action_ledger: {storage: memory, tools: [send_payment]}
tools:
  send_payment:
    callable: already_wrapped_tools:charge
"""
    )
    wrapped = config.apply_tool("send_payment", charge)
    module = _module(monkeypatch, "already_wrapped_tools", charge=wrapped)

    instrument_configured_callables(config)

    assert module.charge is wrapped
    module.charge(amount=1.0, request_id="same")
    module.charge(amount=1.0, request_id="same")
    assert calls == [1.0]


def test_partial_manual_wrapper_fails_closed(monkeypatch) -> None:
    @ledger_sync()
    def charge(amount: float) -> float:
        return amount

    _module(monkeypatch, "partial_tools", charge=charge)
    config = load_config_from_string(
        """
action_ledger: {storage: memory, tools: [send_payment]}
tools:
  send_payment:
    callable: partial_tools:charge
"""
    )

    with pytest.raises(ConfigError, match="partially Mycelium-wrapped"):
        instrument_configured_callables(config)


def test_missing_and_non_callable_targets_fail_closed(monkeypatch) -> None:
    module = ModuleType("invalid_tools")
    module.not_callable = 42
    monkeypatch.setitem(sys.modules, "invalid_tools", module)

    missing = load_config_from_string(
        """
action_ledger: {storage: memory, tools: [missing]}
tools:
  missing:
    callable: invalid_tools:missing
"""
    )
    with pytest.raises(ConfigError, match="does not exist"):
        instrument_configured_callables(missing)

    invalid = load_config_from_string(
        """
action_ledger: {storage: memory, tools: [invalid]}
tools:
  invalid:
    callable: invalid_tools:not_callable
"""
    )
    with pytest.raises(ConfigError, match="is not a function"):
        instrument_configured_callables(invalid)
