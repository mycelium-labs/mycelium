"""
Tests for ContentBlockNormalizer provider format auto-detection.
"""

from __future__ import annotations

from mycelium import ContentBlockNormalizer


class TestDetectFormat:
    def test_detect_openai_by_tool_calls(self) -> None:
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
        detected = ContentBlockNormalizer().detect_format(messages)
        assert detected == "openai"

    def test_detect_openai_by_function_call(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "function_call": {"name": "get_weather", "arguments": "{}"},
            },
        ]
        detected = ContentBlockNormalizer().detect_format(messages)
        assert detected == "openai"

    def test_detect_anthropic_by_thinking(self) -> None:
        messages = [
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "..."}]},
        ]
        detected = ContentBlockNormalizer().detect_format(messages)
        assert detected == "anthropic"

    def test_detect_anthropic_by_tool_use(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tu_1", "name": "get_weather", "input": {}}],
            },
        ]
        detected = ContentBlockNormalizer().detect_format(messages)
        assert detected == "anthropic"

    def test_detect_deepseek_by_think_tags(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "Let me think... <think>Need weather data</think>The weather is sunny.",
            },
        ]
        detected = ContentBlockNormalizer().detect_format(messages)
        assert detected == "deepseek"

    def test_detect_deepseek_by_reasoning_content(self) -> None:
        messages = [
            {
                "role": "assistant",
                "reasoning_content": "Thinking step by step...",
                "content": "The answer is 42.",
            },
        ]
        detected = ContentBlockNormalizer().detect_format(messages)
        assert detected == "deepseek"

    def test_detect_none_for_plain_messages(self) -> None:
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        detected = ContentBlockNormalizer().detect_format(messages)
        assert detected is None


class TestProviderMismatchDetection:
    def test_mismatch_detected_in_audit(self) -> None:
        """OpenAI-format messages sent with target_provider=anthropic emit a warning."""
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="anthropic")
        normalizer.normalize(messages)
        assert any(e["event"] == "provider_format_mismatch" for e in normalizer.audit_log())

    def test_mismatch_not_raised_when_not_strict(self) -> None:
        """Mismatch emits an event but does not raise by default."""
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="anthropic")
        result = normalizer.normalize(messages)
        assert result is not None  # no exception

    def test_mismatch_raises_when_strict(self) -> None:
        """Mismatch emits an event but does not raise (warning-only)."""
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="anthropic", strict=True)
        result = normalizer.normalize(messages)
        assert result is not None  # no exception
        assert any(e["event"] == "provider_format_mismatch" for e in normalizer.audit_log())

    def test_no_mismatch_when_formats_match(self) -> None:
        """OpenAI messages with target_provider=openai emit no mismatch."""
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider="openai")
        normalizer.normalize(messages)
        assert not any(e["event"] == "provider_format_mismatch" for e in normalizer.audit_log())

    def test_no_target_provider_skips_check(self) -> None:
        """Without target_provider, no format check is performed."""
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "x", "arguments": "{}"},
                    }
                ],
            },
        ]
        normalizer = ContentBlockNormalizer(target_provider=None)
        normalizer.normalize(messages)
        assert not any(e["event"] == "provider_format_mismatch" for e in normalizer.audit_log())
