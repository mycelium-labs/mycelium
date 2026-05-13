"""
Real AF-006 reproducer tests from actual GitHub issues.

Each test references a real issue, simulates the scenario described there,
and verifies the Mycelium SDK catches the corruption. Tool calls and LLM
responses are mocked — the SDK guards themselves are tested live.

Sources: HuggingFace dataset ndileep/mycelium-agent-failures predictions.
"""

from __future__ import annotations

import pytest

from mycelium import (
    ContentBlockNormalizer,
    HistoryGuard,
    HistoryTruncatedError,
    MessageValidationError,
    MessageValidator,
    ScratchpadGuard,
    Session,
    ToolSequencer,
    protect,
)

# ---------------------------------------------------------------------------
# 1. Duplicate tool_calls (LangChain #36985)
#    https://github.com/langchain-ai/langchain/issues/36985
#    Streamed tool_call blocks produce fc_* partials alongside call_* finals.
# ---------------------------------------------------------------------------


class TestLangChain36985:
    """Streaming produces mixed fc_* partial + call_* final tool-call blocks."""

    def test_validate_detects_duplicate_blocks(self) -> None:
        messages = [
            {"role": "user", "content": "What's the weather?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "fc_abc123",
                        "function": {"name": "get_weather", "arguments": ""},
                        "type": "function",
                    },
                    {
                        "id": "call_def456",
                        "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                        "type": "function",
                    },
                ],
            },
        ]
        validator = MessageValidator()
        with pytest.raises(MessageValidationError) as exc:
            validator.validate(messages)
        assert exc.value.violation == "duplicate_tool_call_blocks"

    def test_repair_drops_fc_partials(self) -> None:
        messages = [
            {"role": "user", "content": "What's the weather?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "fc_abc123",
                        "function": {"name": "get_weather", "arguments": ""},
                        "type": "function",
                    },
                    {
                        "id": "call_def456",
                        "function": {"name": "get_weather", "arguments": '{"city": "SF"}'},
                        "type": "function",
                    },
                ],
            },
        ]
        repaired = MessageValidator().repair(messages)
        tool_ids = [tc["id"] for tc in repaired[1]["tool_calls"]]
        assert tool_ids == ["call_def456"]


# ---------------------------------------------------------------------------
# 2. Structured output parsed artifacts (LangChain #36916)
#    https://github.com/langchain-ai/langchain/issues/36916
#    Assistant response JSON leaves `parsed` field in message history.
# ---------------------------------------------------------------------------


class TestLangChain36916:
    """parsed field from structured output persists in message history."""

    def test_validate_detects_parsed_artifact(self) -> None:
        messages = [
            {"role": "user", "content": "Extract name and age"},
            {
                "role": "assistant",
                "content": '{"name": "Alice", "age": 30}',
                "parsed": {"name": "Alice", "age": 30},
            },
        ]
        validator = MessageValidator()
        with pytest.raises(MessageValidationError) as exc:
            validator.validate(messages)
        assert exc.value.violation == "parsed_artifact"

    def test_repair_strips_parsed(self) -> None:
        messages = [
            {"role": "assistant", "content": '{"name": "Alice"}', "parsed": {"name": "Alice"}},
        ]
        repaired = MessageValidator().repair(messages)
        assert "parsed" not in repaired[0]


# ---------------------------------------------------------------------------
# 3. Token-limited context drops middle messages (AutoGen #6789)
#    https://github.com/microsoft/autogen/issues/6789
#    TokenLimitedChatCompletionContext removes middle messages.
# ---------------------------------------------------------------------------


class TestAutoGen6789:
    """HistoryGuard detects when middle messages are silently dropped."""

    def test_drop_detection(self) -> None:
        guard = HistoryGuard()
        original = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "My name is Alice and I live in Boston."},
            {"role": "assistant", "content": "Nice to meet you Alice!"},
            {"role": "user", "content": "What's the weather in my city?"},
            {"role": "assistant", "content": "Let me check."},
        ]
        guard.validate(original)

        # Middle messages dropped (simulating TokenLimitedChatCompletionContext)
        truncated = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What's the weather in my city?"},
            {"role": "assistant", "content": "Let me check."},
        ]
        with pytest.raises(HistoryTruncatedError) as exc:
            guard.check_for_drops(truncated)
        assert exc.value.message_count == 3


# ---------------------------------------------------------------------------
# 4. Orphaned reasoning item during handoff (OpenAI Agents #2503)
#    https://github.com/openai/openai-agents-python/issues/2503
#    Handoff serialization produces orphan reasoning item.
# ---------------------------------------------------------------------------


class TestOpenAIAgents2503:
    """Orphaned reasoning content block detected by validator."""

    def test_orphaned_item_after_assistant(self) -> None:
        messages = [
            {"role": "user", "content": "What's the news?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_news", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "No news today."},
            {"role": "assistant", "content": "No news today."},
            {"role": "tool", "tool_call_id": "call_2", "content": "Breaking story!"},
        ]
        validator = MessageValidator()
        with pytest.raises(MessageValidationError) as exc:
            validator.validate(messages)
        assert exc.value.violation == "orphaned_tool_result"

    def test_misplaced_result_detected(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "a", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "First result"},
            {"role": "assistant", "content": "OK, continuing."},
            {"role": "tool", "tool_call_id": "call_1", "content": "Late duplicate"},
        ]
        validator = MessageValidator()
        with pytest.raises(MessageValidationError) as exc:
            validator.validate(messages)
        assert exc.value.violation == "misplaced_tool_result"


# ---------------------------------------------------------------------------
# 5. Multi-agent memory cleared on sub-agent calls (Smolagents #1695)
#    https://github.com/huggingface/smolagents/issues/1695
#    Management agent calls sub-agent, sub-agent memory is reset.
# ---------------------------------------------------------------------------


class TestSmolagents1695:
    """ScratchpadGuard detects when one agent overwrites another's state."""

    def test_cross_agent_overwrite_detected(self) -> None:
        guard = ScratchpadGuard()
        shared = guard.wrap({"task": "analyze data", "deadline": "2026-06-01"}, name="manager")
        shared = guard.wrap(shared, name="worker")
        worker_read = shared["task"]
        assert worker_read == "analyze data"
        shared["task"] = "rewritten by worker"
        assert guard.has_event("scratchpad_overwrite")
        event = [e for e in guard.audit_log() if e["event"] == "scratchpad_overwrite"][0]
        assert event["previous_writer"] == "manager"
        assert event["writer"] == "worker"


# ---------------------------------------------------------------------------
# 6. Premature context condensation (Cline #9599)
#    https://github.com/cline/cline/issues/9599
#    Hardcoded 200k token threshold triggers condensation even at 1M context.
# ---------------------------------------------------------------------------


class TestCline9599:
    """Excessive compaction detection catches premature condensation."""

    def test_excessive_compaction_detected(self) -> None:
        guard = HistoryGuard(max_compaction_ratio=2.0)
        original = [
            {
                "role": "user",
                "content": "We need to build a full-stack application with authentication, database, API endpoints, and a React frontend. The authentication should support OAuth2 with Google and GitHub providers. The database schema needs tables for users, projects, and tasks with proper foreign key relationships.",
            },
            {
                "role": "assistant",
                "content": "I'll help you build that. Let me start with the project structure and dependencies.",
            },
        ]
        guard.validate(original)
        condensed = [
            {"role": "user", "content": "build full-stack app"},
            {"role": "assistant", "content": "starting"},
        ]
        with pytest.raises(HistoryTruncatedError) as exc:
            guard.check_summary_fidelity(condensed)
        assert "Excessive compaction" in str(exc.value)


# ---------------------------------------------------------------------------
# 7. Duplicate messages after checkpoint fork (LangGraph #7593)
#    https://github.com/langchain-ai/langgraph/issues/7593
#    Forking from checkpoint produces duplicate human messages.
# ---------------------------------------------------------------------------


class TestLangGraph7593:
    """Duplicate turn detection catches repeated messages in history."""

    def test_duplicate_human_message_detected(self) -> None:
        guard = HistoryGuard(detect_duplicates=True)
        messages = [
            {"role": "user", "content": "Explain quantum computing"},
            {"role": "assistant", "content": "Quantum computing uses qubits."},
            {"role": "user", "content": "Explain quantum computing"},
        ]
        with pytest.raises(HistoryTruncatedError) as exc:
            guard.validate(messages)
        assert "Duplicate turns" in str(exc.value)


# ---------------------------------------------------------------------------
# 8. Tool results not persisted across turns (OpenAI Agents #2426)
#    https://github.com/openai/openai-agents-python/issues/2426
#    Session state lost between turns.
# ---------------------------------------------------------------------------


class TestOpenAIAgents2426:
    """mark_as_write tracks tool results; next turn sees fresh data."""

    @pytest.mark.asyncio
    async def test_write_then_cross_session_read_bypasses_cache(self) -> None:
        state = {"result": "value1"}

        @protect(entity_param="tool", ttl=60, mark_as_write=True)
        async def run_tool(tool: str) -> dict:
            return {"tool": tool, "result": state["result"]}

        @protect(entity_param="tool", ttl=60, read_after_write_grace=2.0)
        async def get_history(tool: str) -> dict:
            return {"tool": tool, "result": state["result"]}

        async with Session() as s:
            await run_tool(tool="search")
            state["result"] = "value2"
            r = await get_history(tool="search")

        assert r["result"] == "value2"
        assert any(e["event"] == "cache_write_grace_bypass" for e in s.audit_log())


# ---------------------------------------------------------------------------
# 9. Thinking model returns empty content (OpenHands #12058)
#    https://github.com/OpenHands/OpenHands/issues/12058
#    kimi-k2 thinking mode produces empty content, corrupts context.
# ---------------------------------------------------------------------------


class TestOpenHands12058:
    """ContentBlockNormalizer handles empty and thinking-derived content."""

    def test_deepseek_think_blocks_extracted(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "Let me think about this carefully.<think>I need to check the weather API</think>The weather is sunny today.",
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="openai")
        result = normalizer.normalize(messages)
        assert "<think>" not in result[0]["content"]
        assert any(e["event"] == "deepseek_thinking_extracted" for e in normalizer.audit_log())

    def test_anthropic_thinking_blocks_preserved_for_anthropic(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Let me reason..."},
                    {"type": "text", "text": "The answer is 42."},
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="anthropic")
        result = normalizer.normalize(messages)
        assert len(result[0]["content"]) == 2

    def test_anthropic_thinking_blocks_flagged_for_openai(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "text", "text": "Answer."},
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="openai")
        normalizer.normalize(messages)
        assert any(e["event"] == "thinking_block_incompatible" for e in normalizer.audit_log())


# ---------------------------------------------------------------------------
# 10. BinaryOperatorAggregate silently drops values (LangGraph #7590)
#     https://github.com/langchain-ai/langgraph/issues/7590
#     Late-arriving values are silently dropped after Overwrite.
# ---------------------------------------------------------------------------


class TestLangGraph7590:
    """ToolSequencer detects when results arrive out of order."""

    def test_sequencer_detects_out_of_order(self) -> None:
        seq = ToolSequencer()
        id1 = seq.begin("process_a", key="val1")
        id2 = seq.begin("process_b", key="val2")
        id3 = seq.begin("process_c", key="val3")
        seq.end(id3, "process_c")
        seq.end(id1, "process_a")
        seq.end(id2, "process_b")
        out_of_order = [e for e in seq.audit_log() if e["event"] == "tool_result_out_of_order"]
        assert len(out_of_order) >= 2


# ---------------------------------------------------------------------------
# 11. Race condition in RealtimeModel options (LiveKit #5530)
#     https://github.com/livekit/agents/issues/5530
#     update_options() mutates shared state before forwarding.
# ---------------------------------------------------------------------------


class TestLiveKit5530:
    """ScratchpadGuard detects uncoordinated shared state mutations."""

    def test_race_condition_overwrite_detected(self) -> None:
        guard = ScratchpadGuard()
        shared = guard.wrap({"model": "gpt-4", "temperature": 0.7}, name="session")
        shared = guard.wrap(shared, name="update_options")
        shared["model"] = "gpt-4-turbo"
        assert guard.has_event("scratchpad_overwrite")
        shared["temperature"] = 0.5
        overwrites = [e for e in guard.audit_log() if e["event"] == "scratchpad_overwrite"]
        assert len(overwrites) == 2


# ---------------------------------------------------------------------------
# 12. Provider format mismatch (AutoGen #7410)
#     https://github.com/microsoft/autogen/issues/7410
#     Reasoning content blocks passed to non-OpenAI providers.
# ---------------------------------------------------------------------------


class TestAutoGen7410:
    """ContentBlockNormalizer detects provider format mismatches."""

    def test_openai_format_sent_to_anthropic_raises_mismatch(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": "{}"},
                    }
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="anthropic")
        normalizer.normalize(messages)
        assert any(e["event"] == "provider_format_mismatch" for e in normalizer.audit_log())
        event = [e for e in normalizer.audit_log() if e["event"] == "provider_format_mismatch"][0]
        assert event["detected"] == "openai"
        assert event["target"] == "anthropic"

    def test_openai_reasoning_stripped_for_anthropic(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Final answer"},
                    {"type": "reasoning", "reasoning": "thinking..."},
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="anthropic")
        result = normalizer.normalize(messages)
        block_types = [b["type"] for b in result[0]["content"]]
        assert "reasoning" not in block_types

    def test_function_call_normalized_for_anthropic(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "function_call": {"name": "get_weather", "arguments": "{}"},
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="anthropic")
        result = normalizer.normalize(messages)
        assert result[0].get("tool_calls") is not None
        assert "function_call" not in result[0]
