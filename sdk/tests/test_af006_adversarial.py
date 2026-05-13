"""
Adversarial tests — deliberately inject wrong/corrupted data to verify the
SDK catches every corruption attempt.
"""

from __future__ import annotations

import pytest

from mycelium import (
    ContentBlockError,
    ContentBlockNormalizer,
    EntityPatternError,
    HistoryGuard,
    HistoryTruncatedError,
    MessageValidationError,
    MessageValidator,
    ScratchpadGuard,
    Session,
    TenancyMismatchError,
    ToolSequencer,
    protect,
    protect_sync,
)

# ---------------------------------------------------------------------------
# Tool-result corruption
# ---------------------------------------------------------------------------


class TestWrongToolCallId:
    """Feed tool results with mismatched or missing IDs."""

    def test_wrong_id_raises_orphaned(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_real", "function": {"name": "get_weather", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_fake", "content": "72F"},
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator().validate(messages)
        assert e.value.violation == "orphaned_tool_result"

    def test_missing_id_raises(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_1", "function": {"name": "x", "arguments": "{}"}}],
            },
            {"role": "tool", "content": "result"},
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator().validate(messages)
        assert e.value.violation == "missing_tool_call_id"

    def test_swapped_tool_results_detected_as_misplaced(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_a", "function": {"name": "a", "arguments": "{}"}},
                    {"id": "call_b", "function": {"name": "b", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_b", "content": "Result B"},
            {"role": "assistant", "content": "Got B"},
            {"role": "tool", "tool_call_id": "call_a", "content": "Result A"},
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator().validate(messages)
        assert e.value.violation == "misplaced_tool_result"


class TestWrongRole:
    """Messages with deliberately wrong roles."""

    def test_invalid_role_raises(self) -> None:
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "admin", "content": "Invalid role"},
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator(strict_roles=True).validate(messages)
        assert e.value.violation == "invalid_role"

    def test_tool_result_after_irrelevant_assistant_raises_misplaced(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_x", "function": {"name": "x", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call_x", "content": "Ok"},
            {"role": "assistant", "content": "Done with that, moving on."},
            {"role": "tool", "tool_call_id": "call_x", "content": "Duplicate result"},
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator().validate(messages)
        assert e.value.violation == "misplaced_tool_result"


class TestWrongToolCallFormat:
    """Malformed tool-call blocks."""

    def test_duplicate_tool_call_ids_raises(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_dup", "function": {"name": "x", "arguments": "{}"}},
                    {"id": "call_dup", "function": {"name": "y", "arguments": "{}"}},
                ],
            },
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator().validate(messages)
        assert e.value.violation == "duplicate_tool_call_ids"

    def test_mixed_fc_and_call_blocks_raises(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "fc_partial", "function": {"name": "x", "arguments": ""}},
                    {"id": "call_final", "function": {"name": "x", "arguments": "{}"}},
                ],
            },
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator().validate(messages)
        assert e.value.violation == "duplicate_tool_call_blocks"

    def test_nonzero_index_raises(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_1", "index": 1, "function": {"name": "x", "arguments": "{}"}}
                ],
            },
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator().validate(messages)
        assert e.value.violation == "nonzero_tool_call_index"


# ---------------------------------------------------------------------------
# Cache-corruption attempts
# ---------------------------------------------------------------------------


class TestCacheCorruption:
    """Attempt to corrupt or bypass the cache."""

    @pytest.mark.asyncio
    async def test_entity_pattern_validates_format(self) -> None:
        @protect(entity_param="customer_id", entity_pattern=r"^c\d+$")
        async def fetch(customer_id: str) -> dict:
            return {"id": customer_id}

        async with Session():
            with pytest.raises(EntityPatternError):
                await fetch(customer_id="invalid-format")

    @pytest.mark.asyncio
    async def test_entity_tenancy_mismatch_raises(self) -> None:
        @protect(entity_param="customer_id", entity_field="id")
        async def fetch(customer_id: str) -> dict:
            return {"id": "wrong_entity"}

        async with Session():
            with pytest.raises(TenancyMismatchError):
                await fetch(customer_id="c1")

    @pytest.mark.asyncio
    async def test_cache_empty_zero_never_caches_empty(self) -> None:
        calls = [0]

        @protect(entity_param="q", cache_empty=0, ttl=60)
        async def search(q: str) -> list:
            calls[0] += 1
            return []

        async with Session():
            await search(q="missing")
            await search(q="missing")

        assert calls[0] == 2


# ---------------------------------------------------------------------------
# History-corruption attempts
# ---------------------------------------------------------------------------


class TestHistoryCorruption:
    """Deliberately corrupt message history."""

    def test_duplicate_turns_detected(self) -> None:
        guard = HistoryGuard(detect_duplicates=True)
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Hello"},
        ]
        with pytest.raises(HistoryTruncatedError):
            guard.validate(messages)

    def test_dropped_messages_detected(self) -> None:
        guard = HistoryGuard()
        original = [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
            {"role": "user", "content": "C"},
        ]
        guard.validate(original)
        truncated = [
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
        ]
        with pytest.raises(HistoryTruncatedError):
            guard.check_for_drops(truncated)

    def test_token_overflow_detected(self) -> None:
        guard = HistoryGuard(max_tokens=20)
        messages = [
            {
                "role": "user",
                "content": "This is a very long message that far exceeds the token limit we set",
            },
        ]
        with pytest.raises(HistoryTruncatedError):
            guard.validate(messages)


# ---------------------------------------------------------------------------
# Content-block corruption attempts
# ---------------------------------------------------------------------------


class TestContentBlockCorruption:
    """Feed wrong provider formats or malformed content blocks."""

    def test_openai_tool_calls_misattributed_to_anthropic_detected(self) -> None:
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "get_weather", "arguments": "{}"}}
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="anthropic")
        normalizer.normalize(messages)
        assert any(e["event"] == "provider_format_mismatch" for e in normalizer.audit_log())

    def test_thinking_blocks_to_openai_flagged(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "text", "text": "Answer"},
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="openai")
        normalizer.normalize(messages)
        assert any(e["event"] == "thinking_block_incompatible" for e in normalizer.audit_log())

    def test_thinking_blocks_to_openai_strict_raises(self) -> None:
        messages = [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]},
        ]
        normalizer = ContentBlockNormalizer(target_provider="openai", strict=True)
        with pytest.raises(ContentBlockError):
            normalizer.normalize(messages)

    def test_deepseek_think_in_content_extracted(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "Let me think.<think>internal reasoning</think>Final answer",
            },
        ]
        normalizer = ContentBlockNormalizer()
        result = normalizer.normalize(messages)
        assert "<think>" not in result[0]["content"]

    def test_parsed_artifact_detected(self) -> None:
        messages = [
            {"role": "assistant", "content": "JSON output", "parsed": {"key": "value"}},
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator().validate(messages)
        assert e.value.violation == "parsed_artifact"


# ---------------------------------------------------------------------------
# Multi-agent state corruption
# ---------------------------------------------------------------------------


class TestMultiAgentCorruption:
    """One agent corrupting another agent's state."""

    def test_read_before_write_detected(self) -> None:
        guard = ScratchpadGuard()
        shared = guard.wrap({}, name="worker")
        try:
            _ = shared["uninitialized"]
        except KeyError:
            pass
        assert any(e["event"] == "scratchpad_read_before_write" for e in guard.audit_log())

    def test_cross_agent_delete_detected(self) -> None:
        guard = ScratchpadGuard()
        shared = guard.wrap({"key": "value"}, name="creator")
        shared = guard.wrap(shared, name="destroyer")
        del shared["key"]
        events = [e for e in guard.audit_log() if e["event"] == "scratchpad_delete"]
        assert len(events) == 1
        assert events[0]["deleter"] == "destroyer"
        assert events[0]["previous_writer"] == "creator"

    def test_agent_overwrites_anothers_key(self) -> None:
        guard = ScratchpadGuard()
        shared = guard.wrap({}, name="agent_a")
        shared["config"] = "original"
        shared = guard.wrap(shared, name="agent_b")
        shared["config"] = "overwritten"
        assert any(e["event"] == "scratchpad_overwrite" for e in guard.audit_log())


# ---------------------------------------------------------------------------
# Out-of-order tool results
# ---------------------------------------------------------------------------


class TestSequencerCorruption:
    """Tool results arriving in wrong order."""

    def test_three_calls_last_two_out_of_order(self) -> None:
        seq = ToolSequencer()
        id1 = seq.begin("step_a")
        id2 = seq.begin("step_b")
        id3 = seq.begin("step_c")
        seq.end(id3, "step_c")
        seq.end(id1, "step_a")
        seq.end(id2, "step_b")
        ooo = [e for e in seq.audit_log() if e["event"] == "tool_result_out_of_order"]
        assert len(ooo) == 2  # step_a and step_b both arrived after step_c

    def test_all_reversed_order(self) -> None:
        seq = ToolSequencer()
        ids = [seq.begin(f"call_{i}") for i in range(5)]
        for i in reversed(ids):
            seq.end(i, f"call_{i}")
        ooo = [e for e in seq.audit_log() if e["event"] == "tool_result_out_of_order"]
        assert len(ooo) == 4  # 4 of 5 arrived after a later-started call


# ---------------------------------------------------------------------------
# protect_sync corruption
# ---------------------------------------------------------------------------


class TestSyncCorruption:
    """Entity pattern and tenancy validation in sync path."""

    def test_entity_pattern_mismatch(self) -> None:
        from mycelium.protect import _session_var

        @protect_sync(entity_param="customer_id", entity_pattern=r"^c\d+$")
        def fetch(customer_id: str) -> dict:
            return {"id": customer_id}

        token = _session_var.set(Session())
        try:
            with pytest.raises(EntityPatternError):
                fetch(customer_id="bad_id")
        finally:
            _session_var.reset(token)

    def test_tenancy_mismatch(self) -> None:
        from mycelium.protect import _session_var

        @protect_sync(entity_param="customer_id", entity_field="id")
        def fetch(customer_id: str) -> dict:
            return {"id": "wrong"}

        token = _session_var.set(Session())
        try:
            with pytest.raises(TenancyMismatchError):
                fetch(customer_id="c1")
        finally:
            _session_var.reset(token)
