import pytest
from pydantic import BaseModel, Field

from mycelium import (
    ToolBoundaryError,
    ToolBoundaryExhaustedError,
    ToolRegistry,
    ToolRunner,
    bounded,
)


class FetchCustomerInput(BaseModel):
    customer_id: str = Field(min_length=1, pattern=r"^c\d+$")


class CustomerRecord(BaseModel):
    customer_id: str
    name: str


async def test_runner_retries_output_validation() -> None:
    calls = 0

    @bounded(schema=FetchCustomerInput, output_schema=CustomerRecord)
    async def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        if calls < 3:
            return []
        return {"customer_id": customer_id, "name": "Alice"}

    runner = ToolRunner(max_tool_retries=3)
    result = await runner.call(fetch_customer, customer_id="c1")

    assert result == {"customer_id": "c1", "name": "Alice"}
    assert calls == 3


async def test_runner_enforces_allowlist() -> None:
    @bounded(schema=FetchCustomerInput)
    async def fetch_customer(customer_id: str) -> dict:
        return {"customer_id": customer_id}

    registry = ToolRegistry(allowed=["get_orders"])
    runner = ToolRunner(registry=registry)

    with pytest.raises(ToolBoundaryError) as exc:
        await runner.call(fetch_customer, customer_id="c1")

    assert exc.value.violation == "not_in_allowlist"


async def test_runner_llm_retry_recovers_from_bad_input() -> None:
    calls = 0
    llm_calls = 0

    @bounded(schema=FetchCustomerInput)
    async def fetch_customer(customer_id: str) -> dict:
        nonlocal calls
        calls += 1
        return {"customer_id": customer_id}

    async def invoke_llm(messages: list[dict]) -> list[dict]:
        nonlocal llm_calls
        llm_calls += 1
        return messages

    def parse_tool_kwargs(messages: list[dict], tool_name: str) -> dict:
        assert tool_name == "fetch_customer"
        return {"customer_id": "c1"}

    runner = ToolRunner(max_llm_retries=2)
    messages = [{"role": "user", "content": "get customer"}]

    result, updated = await runner.run_with_llm_retry(
        fetch_customer,
        messages=messages,
        tool_call_id="call_1",
        kwargs={"customer_id": None},
        invoke_llm=invoke_llm,
        parse_tool_kwargs=parse_tool_kwargs,
    )

    assert result == {"customer_id": "c1"}
    assert calls == 1
    assert llm_calls == 1
    assert any(m.get("role") == "tool" for m in updated)


async def test_runner_exhausts_llm_retries() -> None:
    @bounded(schema=FetchCustomerInput)
    async def fetch_customer(customer_id: str) -> dict:
        return {"customer_id": customer_id}

    async def invoke_llm(messages: list[dict]) -> list[dict]:
        return messages

    def parse_tool_kwargs(messages: list[dict], tool_name: str) -> dict:
        return {"customer_id": None}

    runner = ToolRunner(max_llm_retries=1)

    with pytest.raises(ToolBoundaryExhaustedError) as exc:
        await runner.run_with_llm_retry(
            fetch_customer,
            messages=[],
            tool_call_id="call_1",
            kwargs={"customer_id": None},
            invoke_llm=invoke_llm,
            parse_tool_kwargs=parse_tool_kwargs,
        )

    assert exc.value.last_error.violation in {"type_mismatch", "string_type"}
