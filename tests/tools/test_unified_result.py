#!/usr/bin/env python3
"""
Tests for UnifiedResult dataclass and fallback utilities.

Covers:
- UnifiedResult creation and serialization
- Fallback detection and path formatting
- Exponential backoff delay calculation
- Fallback logging
"""

import json
import pytest
from unittest.mock import patch

from tools.delegate_tool import (
    UnifiedResult,
    _calculate_fallback_delay,
    _log_fallback,
    FALLBACK_INITIAL_DELAY,
    FALLBACK_MAX_DELAY,
    FALLBACK_BACKOFF_FACTOR,
    MAX_FALLBACK_DEPTH,
)


class TestUnifiedResult:
    """Tests for UnifiedResult dataclass."""

    def test_default_creation(self):
        """Should create with default values."""
        result = UnifiedResult()
        assert result.success is False
        assert result.output == ""
        assert result.agent_type == ""
        assert result.fallback_depth == 0
        assert result.is_fallback() is False

    def test_successful_result(self):
        """Should create successful result."""
        result = UnifiedResult(
            success=True,
            output="Task completed",
            agent_type="hermes",
            duration=5.0,
            api_calls=3
        )
        assert result.success is True
        assert result.output == "Task completed"
        assert result.agent_type == "hermes"
        assert result.duration == 5.0

    def test_fallback_detection(self):
        """Should detect fallback when depth > 0."""
        no_fallback = UnifiedResult(fallback_depth=0)
        assert no_fallback.is_fallback() is False

        with_fallback = UnifiedResult(fallback_depth=1)
        assert with_fallback.is_fallback() is True

        deep_fallback = UnifiedResult(fallback_depth=3)
        assert deep_fallback.is_fallback() is True

    def test_execution_path_no_fallback(self):
        """Should return agent_type when no fallback."""
        result = UnifiedResult(agent_type="hermes", fallback_depth=0)
        assert result.get_execution_path() == "hermes"

    def test_execution_path_with_fallback(self):
        """Should format path with fallback info."""
        result = UnifiedResult(
            original_agent="opencode",
            agent_type="cline",
            fallback_depth=1
        )
        path = result.get_execution_path()
        assert "opencode" in path
        assert "cline" in path
        assert "폴백" in path

    def test_to_json_serialization(self):
        """Should serialize to valid JSON."""
        result = UnifiedResult(
            success=True,
            output="test output",
            agent_type="hermes",
            original_agent="opencode",
            fallback_depth=1,
            fallback_delay=2.0,
            duration=5.5,
            api_calls=10,
            error=None
        )
        json_str = result.to_json()

        # Should be valid JSON
        parsed = json.loads(json_str)
        assert parsed["success"] is True
        assert parsed["output"] == "test output"
        assert parsed["agent_type"] == "hermes"
        assert parsed["fallback_depth"] == 1

    def test_from_dict_deserialization(self):
        """Should deserialize from dictionary."""
        data = {
            "success": True,
            "output": "test",
            "agent_type": "codex",
            "original_agent": "cline",
            "fallback_depth": 2,
            "fallback_delay": 4.0,
            "duration": 10.0,
            "api_calls": 5,
            "error": None
        }
        result = UnifiedResult.from_dict(data)

        assert result.success is True
        assert result.output == "test"
        assert result.agent_type == "codex"
        assert result.original_agent == "cline"
        assert result.fallback_depth == 2
        assert result.fallback_delay == 4.0

    def test_roundtrip_serialization(self):
        """Should survive serialize-deserialize cycle."""
        original = UnifiedResult(
            success=True,
            output="roundtrip test",
            agent_type="hermes",
            fallback_depth=1
        )
        json_str = original.to_json()
        parsed_dict = json.loads(json_str)
        restored = UnifiedResult.from_dict(parsed_dict)

        assert restored.success == original.success
        assert restored.output == original.output
        assert restored.agent_type == original.agent_type
        assert restored.fallback_depth == original.fallback_depth


class TestFallbackDelayCalculation:
    """Tests for exponential backoff delay calculation."""

    def test_first_fallback_delay(self):
        """First fallback should use initial delay."""
        delay = _calculate_fallback_delay(1)
        assert delay == FALLBACK_INITIAL_DELAY

    def test_second_fallback_delay(self):
        """Second fallback should double the delay."""
        delay = _calculate_fallback_delay(2)
        expected = FALLBACK_INITIAL_DELAY * FALLBACK_BACKOFF_FACTOR
        assert delay == expected

    def test_third_fallback_delay(self):
        """Third fallback should quadruple the delay."""
        delay = _calculate_fallback_delay(3)
        expected = FALLBACK_INITIAL_DELAY * (FALLBACK_BACKOFF_FACTOR ** 2)
        assert delay == expected

    def test_max_delay_cap(self):
        """Delay should not exceed max delay."""
        # Calculate delay for depth that would exceed max
        large_depth = 10
        delay = _calculate_fallback_delay(large_depth)
        assert delay <= FALLBACK_MAX_DELAY

    def test_delay_progression(self):
        """Delays should increase exponentially up to max."""
        delays = [_calculate_fallback_delay(d) for d in range(1, 6)]

        # Check exponential growth
        assert delays[1] == delays[0] * FALLBACK_BACKOFF_FACTOR
        assert delays[2] == delays[1] * FALLBACK_BACKOFF_FACTOR

        # All delays should be <= max
        for d in delays:
            assert d <= FALLBACK_MAX_DELAY


class TestFallbackLogging:
    """Tests for fallback logging function."""

    def test_log_fallback_calls_logger(self):
        """Should call logger with appropriate messages."""
        with patch('tools.delegate_tool.logger') as mock_logger:
            _log_fallback(
                original_agent="opencode",
                fallback_agent="cline",
                reason="Timeout after 30s",
                delay=2.0,
                depth=1
            )

            # Should call warning 3 times (header + details)
            assert mock_logger.warning.call_count >= 1

            # Check first call contains fallback info
            first_call = mock_logger.warning.call_args_list[0]
            assert "FALLBACK" in str(first_call)
            assert "opencode" in str(first_call)
            assert "cline" in str(first_call)

    def test_log_fallback_includes_reason(self):
        """Should include failure reason in log."""
        with patch('tools.delegate_tool.logger') as mock_logger:
            _log_fallback(
                original_agent="codex",
                fallback_agent="opencode",
                reason="API rate limit exceeded",
                delay=2.0,
                depth=1
            )

            # Check that reason appears in log
            log_output = " ".join(str(call) for call in mock_logger.warning.call_args_list)
            assert "API rate limit exceeded" in log_output

    def test_log_fallback_includes_delay(self):
        """Should include delay information."""
        with patch('tools.delegate_tool.logger') as mock_logger:
            _log_fallback(
                original_agent="opencode",
                fallback_agent="hermes",
                reason="Connection error",
                delay=4.0,
                depth=2
            )

            log_output = " ".join(str(call) for call in mock_logger.warning.call_args_list)
            assert "4.0" in log_output or "4" in log_output


class TestFallbackConstants:
    """Tests for fallback-related constants."""

    def test_initial_delay_positive(self):
        """Initial delay should be positive."""
        assert FALLBACK_INITIAL_DELAY > 0

    def test_max_delay_greater_than_initial(self):
        """Max delay should be greater than initial delay."""
        assert FALLBACK_MAX_DELAY > FALLBACK_INITIAL_DELAY

    def test_backoff_factor_greater_than_one(self):
        """Backoff factor should be > 1 for exponential growth."""
        assert FALLBACK_BACKOFF_FACTOR > 1

    def test_max_fallback_depth_positive(self):
        """Max fallback depth should be positive."""
        assert MAX_FALLBACK_DEPTH > 0


class TestIntegrationWithDelegateTask:
    """Integration tests for UnifiedResult in delegation flow."""

    def test_result_structure_matches_schema(self):
        """Result should match expected schema for downstream processing."""
        result = UnifiedResult(
            success=True,
            output="test",
            agent_type="hermes",
            original_agent="opencode",
            fallback_depth=1,
            fallback_delay=2.0,
            duration=5.0,
            api_calls=3,
            tool_trace=[{"tool": "read_file"}],
            error=None
        )

        # Verify all expected fields exist
        assert hasattr(result, 'success')
        assert hasattr(result, 'output')
        assert hasattr(result, 'agent_type')
        assert hasattr(result, 'original_agent')
        assert hasattr(result, 'fallback_depth')
        assert hasattr(result, 'fallback_delay')
        assert hasattr(result, 'duration')
        assert hasattr(result, 'api_calls')
        assert hasattr(result, 'tool_trace')
        assert hasattr(result, 'error')

    def test_tool_trace_can_store_multiple_calls(self):
        """Tool trace should store multiple tool calls."""
        trace = [
            {"tool": "read_file", "args": {"path": "test.py"}},
            {"tool": "write_file", "args": {"path": "output.py"}},
        ]
        result = UnifiedResult(tool_trace=trace)

        assert len(result.tool_trace) == 2
        assert result.tool_trace[0]["tool"] == "read_file"
        assert result.tool_trace[1]["tool"] == "write_file"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
