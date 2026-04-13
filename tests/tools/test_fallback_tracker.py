#!/usr/bin/env python3
"""
Tests for FallbackTracker - execution statistics and fallback chain analysis.
"""

import json
import pytest
from tools.delegate_tool import FallbackTracker, get_fallback_tracker, reset_fallback_tracker


class TestFallbackTracker:
    """Tests for FallbackTracker class."""

    def test_empty_tracker_stats(self):
        """Empty tracker should return zero stats."""
        tracker = FallbackTracker()
        stats = tracker.get_stats()
        
        assert stats["total_attempts"] == 0
        assert stats["fallback_count"] == 0
        assert stats["fallback_rate"] == 0.0
        assert stats["avg_delay"] == 0.0
        assert stats["agent_reliability"] == {}

    def test_record_single_attempt(self):
        """Should record a single successful attempt."""
        tracker = FallbackTracker()
        tracker.record_attempt("opencode", "opencode", "none", 0.0, success=True)
        
        stats = tracker.get_stats()
        assert stats["total_attempts"] == 1
        assert stats["fallback_count"] == 0  # No fallback
        assert stats["fallback_rate"] == 0.0

    def test_record_with_fallback(self):
        """Should detect fallback when agents differ."""
        tracker = FallbackTracker()
        tracker.record_attempt("opencode", "cline", "timeout", 2.0, success=True)
        
        stats = tracker.get_stats()
        assert stats["total_attempts"] == 1
        assert stats["fallback_count"] == 1
        assert stats["fallback_rate"] == 1.0
        assert stats["avg_delay"] == 2.0

    def test_agent_reliability_calculation(self):
        """Should calculate per-agent success rates."""
        tracker = FallbackTracker()
        
        # Use unique agent name to avoid shared state
        unique_agent = "test_agent_reliability"
        tracker.record_attempt(unique_agent, unique_agent, "none", 0.0, success=True)
        tracker.record_attempt(unique_agent, unique_agent, "none", 0.0, success=True)
        tracker.record_attempt(unique_agent, unique_agent, "none", 0.0, success=False)
        
        stats = tracker.get_stats()
        agent_stats = stats["agent_reliability"][unique_agent]
        assert agent_stats["attempts"] == 3
        assert agent_stats["failures"] == 1
        assert agent_stats["success_rate"] == pytest.approx(2/3)

    def test_multiple_fallback_chains(self):
        """Should track multiple different fallback chains."""
        tracker = FallbackTracker()
        
        tracker.record_attempt("opencode", "cline", "timeout", 2.0)
        tracker.record_attempt("opencode", "cline", "timeout", 2.0)
        tracker.record_attempt("opencode", "hermes", "error", 6.0)
        tracker.record_attempt("codex", "opencode", "rate_limit", 2.0)
        
        chains = tracker.get_fallback_chain_report()
        
        # Should have 3 different chains
        assert len(chains) == 3
        
        # Most common first
        assert chains[0]["chain"] == "opencode → cline"
        assert chains[0]["count"] == 2

    def test_fallback_chain_percentage(self):
        """Chain percentages should sum to fallback rate."""
        tracker = FallbackTracker()
        
        # 4 total: 2 with fallback, 2 direct
        tracker.record_attempt("opencode", "cline", "timeout", 2.0)
        tracker.record_attempt("opencode", "hermes", "error", 6.0)
        tracker.record_attempt("codex", "codex", "none", 0.0)
        tracker.record_attempt("claude", "claude", "none", 0.0)
        
        chains = tracker.get_fallback_chain_report()
        total_percentage = sum(c["percentage"] for c in chains)
        
        # Sum of chain percentages = fallback rate
        assert total_percentage == pytest.approx(0.5)

    def test_to_json_serialization(self):
        """Should serialize to valid JSON."""
        tracker = FallbackTracker()
        tracker.record_attempt("opencode", "cline", "timeout", 2.0, success=True)
        
        json_str = tracker.to_json()
        parsed = json.loads(json_str)
        
        assert "stats" in parsed
        assert "fallback_chains" in parsed
        assert parsed["stats"]["total_attempts"] == 1

    def test_global_tracker_singleton(self):
        """Global tracker should be singleton."""
        reset_fallback_tracker()
        
        tracker1 = get_fallback_tracker()
        tracker2 = get_fallback_tracker()
        
        assert tracker1 is tracker2

    def test_global_tracker_reset(self):
        """Reset should create new tracker instance."""
        tracker1 = get_fallback_tracker()
        tracker1.record_attempt("opencode", "cline", "timeout", 2.0)
        
        reset_fallback_tracker()
        tracker2 = get_fallback_tracker()
        
        assert tracker1 is not tracker2
        assert tracker2.get_stats()["total_attempts"] == 0

    def test_delay_accumulation(self):
        """Should accumulate total delay across attempts."""
        tracker = FallbackTracker()
        
        tracker.record_attempt("opencode", "cline", "timeout", 2.0)
        tracker.record_attempt("opencode", "hermes", "error", 6.0)
        
        stats = tracker.get_stats()
        assert stats["avg_delay"] == 4.0  # (2.0 + 6.0) / 2

    def test_no_fallback_for_same_agent(self):
        """Same agent should not count as fallback."""
        tracker = FallbackTracker()
        
        tracker.record_attempt("hermes", "hermes", "none", 0.0)
        tracker.record_attempt("hermes", "hermes", "none", 0.0)
        
        stats = tracker.get_stats()
        assert stats["fallback_count"] == 0
        assert stats["fallback_rate"] == 0.0


class TestFallbackTrackerEdgeCases:
    """Edge case tests for FallbackTracker."""

    def test_empty_chain_report(self):
        """Empty tracker should return empty chain report."""
        tracker = FallbackTracker()
        chains = tracker.get_fallback_chain_report()
        assert chains == []

    def test_zero_delay_attempts(self):
        """Should handle zero delay attempts correctly."""
        tracker = FallbackTracker()
        
        tracker.record_attempt("hermes", "hermes", "none", 0.0)
        
        stats = tracker.get_stats()
        assert stats["avg_delay"] == 0.0

    def test_large_delay_values(self):
        """Should handle large delay values."""
        tracker = FallbackTracker()
        
        tracker.record_attempt("opencode", "hermes", "deep_fallback", 1000.0)
        
        stats = tracker.get_stats()
        assert stats["avg_delay"] == 1000.0

    def test_many_attempts_performance(self):
        """Should handle many attempts efficiently."""
        tracker = FallbackTracker()
        
        for i in range(1000):
            tracker.record_attempt("agent1", "agent2", "reason", 1.0)
        
        stats = tracker.get_stats()
        assert stats["total_attempts"] == 1000
        assert stats["fallback_count"] == 1000


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
