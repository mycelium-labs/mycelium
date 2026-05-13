"""
Adversarial tests — deliberately inject wrong/corrupted data to verify the
SDK catches every corruption attempt.

Run with: uv run pytest tests/test_af006_adversarial.py -v -s
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

_ATTACK_PREFIX = "\n  🛡️  Mycelium protection layer:"
_ATTACK_DESC = "\n  ⚔️  Attack:"


def _print_defense(layer: str, detail: str) -> None:
    print(f"{_ATTACK_PREFIX} {layer}")
    print(f"     {detail}")


# ---------------------------------------------------------------------------
# Tool-result corruption
# ---------------------------------------------------------------------------


class TestWrongToolCallId:
    """Protection: MessageValidator — orphaned/misplaced tool-result detection."""

    def test_wrong_id_raises_orphaned(self) -> None:
        print(f"{_ATTACK_DESC} Tool result with tool_call_id that doesn't match any call")
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
        _print_defense(
            "MessageValidator.validate()",
            f"Caught orphaned tool result → violation={e.value.violation!r}",
        )
        assert e.value.violation == "orphaned_tool_result"

    def test_missing_id_raises(self) -> None:
        print(f"{_ATTACK_DESC} Tool result without tool_call_id field")
        messages = [
            {
                "role": "assistant",
                "tool_calls": [{"id": "call_1", "function": {"name": "x", "arguments": "{}"}}],
            },
            {"role": "tool", "content": "result"},
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator().validate(messages)
        _print_defense(
            "MessageValidator.validate()",
            f"Caught missing tool_call_id → violation={e.value.violation!r}",
        )
        assert e.value.violation == "missing_tool_call_id"

    def test_swapped_tool_results_detected_as_misplaced(self) -> None:
        print(
            f"{_ATTACK_DESC} Tool results returned in wrong order (B before A), with assistant response between"
        )
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
        _print_defense(
            "MessageValidator.validate()",
            f"Caught misplaced tool result → violation={e.value.violation!r}",
        )
        assert e.value.violation == "misplaced_tool_result"


class TestWrongRole:
    """Protection: MessageValidator — role validation."""

    def test_invalid_role_raises(self) -> None:
        print(f"{_ATTACK_DESC} Message with invalid role 'admin' injected into history")
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "admin", "content": "Invalid role"},
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator(strict_roles=True).validate(messages)
        _print_defense(
            "MessageValidator.validate()", f"Caught invalid role → violation={e.value.violation!r}"
        )
        assert e.value.violation == "invalid_role"


class TestWrongToolCallFormat:
    """Protection: MessageValidator — tool-call format validation + repair."""

    def test_duplicate_tool_call_ids_raises(self) -> None:
        print(f"{_ATTACK_DESC} Two tool_calls with the same id in one assistant message")
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
        _print_defense(
            "MessageValidator.validate()",
            f"Caught duplicate tool_call ids → violation={e.value.violation!r}",
        )
        assert e.value.violation == "duplicate_tool_call_ids"

    def test_mixed_fc_and_call_blocks_raises(self) -> None:
        print(
            f"{_ATTACK_DESC} LangChain streaming left both fc_* partials and call_* finals in history"
        )
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
        _print_defense(
            "MessageValidator.validate()",
            f"Caught mixed fc_*/call_* blocks → violation={e.value.violation!r}",
        )

        repaired = MessageValidator().repair(messages)
        _print_defense(
            "MessageValidator.repair()",
            f"Dropped fc_* partial, kept: {[tc['id'] for tc in repaired[0]['tool_calls']]}",
        )
        assert e.value.violation == "duplicate_tool_call_blocks"

    def test_nonzero_index_raises(self) -> None:
        print(f"{_ATTACK_DESC} Tool-call index starts at 1 instead of 0")
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
        _print_defense(
            "MessageValidator.validate()", f"Caught nonzero index → violation={e.value.violation!r}"
        )
        assert e.value.violation == "nonzero_tool_call_index"


# ---------------------------------------------------------------------------
# Cache-corruption attempts
# ---------------------------------------------------------------------------


class TestCacheCorruption:
    """Protection: @protect decorator + Session — cache isolation and validation."""

    @pytest.mark.asyncio
    async def test_entity_pattern_validates_format(self) -> None:
        print(f"{_ATTACK_DESC} Pass 'invalid-format' as customer_id when pattern requires c followed by digits")

        @protect(entity_param="customer_id", entity_pattern=r"^c\d+$")
        async def fetch(customer_id: str) -> dict:
            return {"id": customer_id}

        async with Session():
            with pytest.raises(EntityPatternError) as e:
                await fetch(customer_id="invalid-format")
            _print_defense("@protect(entity_pattern=...)", f"Caught bad entity format → {e.value}")
        assert True

    @pytest.mark.asyncio
    async def test_entity_tenancy_mismatch_raises(self) -> None:
        print(f"{_ATTACK_DESC} Backend returns wrong customer's data (id mismatch)")

        @protect(entity_param="customer_id", entity_field="id")
        async def fetch(customer_id: str) -> dict:
            return {"id": "wrong_entity"}

        async with Session():
            with pytest.raises(TenancyMismatchError) as e:
                await fetch(customer_id="c1")
            _print_defense("@protect(entity_field=...)", f"Caught tenancy mismatch → {e.value}")
        assert True

    @pytest.mark.asyncio
    async def test_cache_empty_zero_never_caches_empty(self) -> None:
        print(
            f"{_ATTACK_DESC} Empty result ([]) from backend — should not be cached when cache_empty=0"
        )
        calls = [0]

        @protect(entity_param="q", cache_empty=0, ttl=60)
        async def search(q: str) -> list:
            calls[0] += 1
            return []

        async with Session():
            await search(q="missing")
            await search(q="missing")

        _print_defense(
            "@protect(cache_empty=0)",
            f"Backend called {calls[0]}x instead of 1x (empty result was never cached)",
        )
        assert calls[0] == 2


# ---------------------------------------------------------------------------
# History-corruption attempts
# ---------------------------------------------------------------------------


class TestHistoryCorruption:
    """Protection: HistoryGuard — message history integrity."""

    def test_duplicate_turns_detected(self) -> None:
        print(f"{_ATTACK_DESC} Same user message 'Hello' duplicated in conversation history")
        guard = HistoryGuard(detect_duplicates=True)
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
            {"role": "user", "content": "Hello"},
        ]
        with pytest.raises(HistoryTruncatedError):
            guard.validate(messages)
        _print_defense(
            "HistoryGuard(detect_duplicates=True)", "Caught duplicate turn → matched by fingerprint"
        )

    def test_dropped_messages_detected(self) -> None:
        print(
            f"{_ATTACK_DESC} Middle messages removed from history (simulating token-limiting middleware)"
        )
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
        with pytest.raises(HistoryTruncatedError) as e:
            guard.check_for_drops(truncated)
        _print_defense(
            "HistoryGuard.check_for_drops()",
            f"Caught {e.value.message_count} dropped messages via fingerprint diff",
        )

    def test_token_overflow_detected(self) -> None:
        print(f"{_ATTACK_DESC} Message history exceeds max_tokens limit")
        guard = HistoryGuard(max_tokens=20)
        messages = [
            {
                "role": "user",
                "content": "This is a very long message that far exceeds the token limit we set",
            },
        ]
        with pytest.raises(HistoryTruncatedError):
            guard.validate(messages)
        _print_defense("HistoryGuard(max_tokens=20)", "Caught token overflow before LLM call")


# ---------------------------------------------------------------------------
# Content-block corruption attempts
# ---------------------------------------------------------------------------


class TestContentBlockCorruption:
    """Protection: ContentBlockNormalizer — provider-specific content validation."""

    def test_openai_tool_calls_misattributed_to_anthropic_detected(self) -> None:
        print(f"{_ATTACK_DESC} OpenAI-format tool_calls sent with target_provider='anthropic'")
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
        events = [e for e in normalizer.audit_log() if e["event"] == "provider_format_mismatch"]
        _print_defense(
            "ContentBlockNormalizer.detect_format()",
            f"Detected OpenAI format → emitted {events[0]['event']!r}",
        )
        assert events

    def test_thinking_blocks_to_openai_flagged(self) -> None:
        print(f"{_ATTACK_DESC} Anthropic thinking blocks sent to OpenAI endpoint")
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
        events = [e for e in normalizer.audit_log() if e["event"] == "thinking_block_incompatible"]
        _print_defense(
            "ContentBlockNormalizer(preserve_thinking=True)",
            f"Flagged incompatible thinking blocks → {events[0]['event']!r}",
        )
        assert events

    def test_thinking_blocks_to_openai_strict_raises(self) -> None:
        print(
            f"{_ATTACK_DESC} Thinking blocks to OpenAI with strict=True → raises ContentBlockError"
        )
        messages = [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]},
        ]
        normalizer = ContentBlockNormalizer(target_provider="openai", strict=True)
        with pytest.raises(ContentBlockError):
            normalizer.normalize(messages)
        _print_defense(
            "ContentBlockNormalizer(strict=True)", "Raised ContentBlockError — prevented data loss"
        )

    def test_deepseek_think_in_content_extracted(self) -> None:
        print(f"{_ATTACK_DESC} DeepSeek-r1 <think> tags left in assistant response text")
        messages = [
            {
                "role": "assistant",
                "content": "Let me think.<think>internal reasoning</think>Final answer",
            },
        ]
        normalizer = ContentBlockNormalizer()
        result = normalizer.normalize(messages)
        _print_defense(
            "ContentBlockNormalizer(extract_deepseek=True)",
            f"Stripped <think> block. Clean content: {result[0]['content'][:30]}...",
        )
        assert "<think>" not in result[0]["content"]

    def test_parsed_artifact_detected(self) -> None:
        print(f"{_ATTACK_DESC} OpenAI structured output `parsed` field left in message history")
        messages = [
            {"role": "assistant", "content": "JSON output", "parsed": {"key": "value"}},
        ]
        with pytest.raises(MessageValidationError) as e:
            MessageValidator().validate(messages)
        _print_defense(
            "MessageValidator(check_parsed=True)",
            f"Caught parsed artifact → violation={e.value.violation!r}",
        )
        assert e.value.violation == "parsed_artifact"


# ---------------------------------------------------------------------------
# Multi-agent state corruption
# ---------------------------------------------------------------------------


class TestMultiAgentCorruption:
    """Protection: ScratchpadGuard — shared state access logging."""

    def test_read_before_write_detected(self) -> None:
        print(f"{_ATTACK_DESC} Agent reads a key that was never initialized in shared state")
        guard = ScratchpadGuard()
        shared = guard.wrap({}, name="worker")
        try:
            _ = shared["uninitialized"]
        except KeyError:
            pass
        events = [e for e in guard.audit_log() if e["event"] == "scratchpad_read_before_write"]
        _print_defense(
            "ScratchpadGuard.__getitem__()",
            f"Logged read-before-write on key 'uninitialized' → event={events[0]['event']!r}",
        )
        assert events

    def test_cross_agent_delete_detected(self) -> None:
        print(f"{_ATTACK_DESC} Agent 'destroyer' deletes a key created by agent 'creator'")
        guard = ScratchpadGuard()
        shared = guard.wrap({"key": "value"}, name="creator")
        shared = guard.wrap(shared, name="destroyer")
        del shared["key"]
        events = [e for e in guard.audit_log() if e["event"] == "scratchpad_delete"]
        _print_defense(
            "ScratchpadGuard.__delitem__()",
            f"Logged cross-agent delete: deleter={events[0]['deleter']!r}, previous writer={events[0]['previous_writer']!r}",
        )
        assert events[0]["deleter"] == "destroyer"
        assert events[0]["previous_writer"] == "creator"

    def test_agent_overwrites_anothers_key(self) -> None:
        print(
            f"{_ATTACK_DESC} Agent 'agent_b' overwrites 'agent_a''s config key without coordination"
        )
        guard = ScratchpadGuard()
        shared = guard.wrap({}, name="agent_a")
        shared["config"] = "original"
        shared = guard.wrap(shared, name="agent_b")
        shared["config"] = "overwritten"
        events = [e for e in guard.audit_log() if e["event"] == "scratchpad_overwrite"]
        _print_defense(
            "ScratchpadGuard.__setitem__()",
            f"Logged uncoordinated overwrite: {events[0]['writer']!r} overwrote {events[0]['previous_writer']!r}'s key",
        )
        assert events


# ---------------------------------------------------------------------------
# Out-of-order tool results
# ---------------------------------------------------------------------------


class TestSequencerCorruption:
    """Protection: ToolSequencer — parallel tool call ordering."""

    def test_three_calls_last_two_out_of_order(self) -> None:
        print(f"{_ATTACK_DESC} Three tools called in order 1→2→3, but results arrive 3→1→2")
        seq = ToolSequencer()
        id1 = seq.begin("step_a")
        id2 = seq.begin("step_b")
        id3 = seq.begin("step_c")
        seq.end(id3, "step_c")
        seq.end(id1, "step_a")
        seq.end(id2, "step_b")
        ooo = [e for e in seq.audit_log() if e["event"] == "tool_result_out_of_order"]
        _print_defense(
            "ToolSequencer.end()",
            f"Logged {len(ooo)} out-of-order result(s) — steps completed after later-started calls",
        )
        assert len(ooo) == 2

    def test_all_reversed_order(self) -> None:
        print(f"{_ATTACK_DESC} Five tools called forward, all results arrive in reverse")
        seq = ToolSequencer()
        ids = [seq.begin(f"call_{i}") for i in range(5)]
        for i in reversed(ids):
            seq.end(i, f"call_{i}")
        ooo = [e for e in seq.audit_log() if e["event"] == "tool_result_out_of_order"]
        _print_defense(
            "ToolSequencer.end()", f"Logged {len(ooo)} out-of-order out of {len(ids)} calls"
        )
        assert len(ooo) == 4


# ---------------------------------------------------------------------------
# Sync path corruption
# ---------------------------------------------------------------------------


class TestSyncCorruption:
    """Protection: @protect_sync — entity validation in synchronous frameworks."""

    def test_entity_pattern_mismatch(self) -> None:
        from mycelium.protect import _session_var

        print(f"{_ATTACK_DESC} protect_sync: entity pattern mismatch with customer_id='bad_id'")

        @protect_sync(entity_param="customer_id", entity_pattern=r"^c\d+$")
        def fetch(customer_id: str) -> dict:
            return {"id": customer_id}

        token = _session_var.set(Session())
        try:
            with pytest.raises(EntityPatternError):
                fetch(customer_id="bad_id")
        finally:
            _session_var.reset(token)
        _print_defense("@protect_sync(entity_pattern=...)", "Caught bad entity format in sync path")

    def test_tenancy_mismatch(self) -> None:
        from mycelium.protect import _session_var

        print(f"{_ATTACK_DESC} protect_sync: backend returns wrong tenancy")

        @protect_sync(entity_param="customer_id", entity_field="id")
        def fetch(customer_id: str) -> dict:
            return {"id": "wrong"}

        token = _session_var.set(Session())
        try:
            with pytest.raises(TenancyMismatchError):
                fetch(customer_id="c1")
        finally:
            _session_var.reset(token)
        _print_defense("@protect_sync(entity_field=...)", "Caught tenancy mismatch in sync path")
