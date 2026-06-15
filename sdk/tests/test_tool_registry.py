import pytest

from mycelium import ToolBoundaryError, ToolRegistry


def test_registry_allows_registered_tool() -> None:
    registry = ToolRegistry(allowed=["fetch_customer", "get_orders"])
    registry.validate_call("fetch_customer")


def test_registry_blocks_unlisted_tool() -> None:
    registry = ToolRegistry(allowed=["fetch_customer"])

    with pytest.raises(ToolBoundaryError) as exc:
        registry.validate_call("delete_git_repo")

    assert exc.value.violation == "not_in_allowlist"
    assert "fetch_customer" in exc.value.llm_message
    assert "delete_git_repo" in exc.value.llm_message


def test_register_adds_function_name() -> None:
    registry = ToolRegistry()

    @registry.register
    def fetch_customer(customer_id: str) -> dict:
        return {}

    registry.validate_call("fetch_customer")

    with pytest.raises(ToolBoundaryError):
        registry.validate_call("other_tool")
