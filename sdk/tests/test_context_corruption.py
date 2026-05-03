"""
Tests for AF-006 context corruption protection.

Verifies:
1. Cache hit/miss logic
2. TTL enforcement
3. Criticality re-check
4. Entity segmentation
5. Error invalidation
6. Audit trail
"""

import pytest

from mycelium.protections.context_corruption import (
    ContextCache,
    ContextSegmentation,
    Criticality,
    InvalidationPolicy,
)


class TestCacheBasics:
    """Test basic cache operations."""

    def test_add_and_get_hit(self):
        policy = InvalidationPolicy(default_ttl_steps=5)
        cache = ContextCache(policy)

        # Add entry
        version_id = cache.add(
            name="user_data",
            value={"id": "alice", "name": "Alice"},
            source="fetch_user",
            entity_id="alice",
            criticality=Criticality.LOW,
        )

        # Get (should hit)
        decision = cache.get("user_data", "fetch_user", "alice")
        assert decision.value == {"id": "alice", "name": "Alice"}
        assert decision.should_refetch is False
        assert decision.access_count == 1
        assert decision.current_version.version_id == version_id

    def test_get_missing(self):
        policy = InvalidationPolicy()
        cache = ContextCache(policy)

        decision = cache.get("nonexistent", "source", None)
        assert decision.value is None
        assert decision.should_refetch is True
        assert decision.access_count == 0

    def test_ttl_expiration(self):
        policy = InvalidationPolicy(default_ttl_steps=3)
        cache = ContextCache(policy)

        cache.add("data", {"v": 1}, "source", None, Criticality.LOW, invalidate_after_steps=3)

        # Step 0: Fresh
        decision = cache.get("data", "source", None)
        assert decision.should_refetch is False

        # Step 3: Age = 3, TTL = 3, should expire
        cache.advance_step()
        cache.advance_step()
        cache.advance_step()
        decision = cache.get("data", "source", None)
        assert decision.should_refetch is True
        assert decision.reason.startswith("Stale")

    def test_criticality_recheck(self):
        policy = InvalidationPolicy(criticality_recheck_threshold=2)
        cache = ContextCache(policy)

        cache.add("critical_data", {"v": 1}, "source", None, Criticality.HIGH)

        # Read 1: Fresh, no refetch
        decision = cache.get("critical_data", "source", None)
        assert decision.should_refetch is False
        assert decision.access_count == 1

        # Read 2: Still fresh by age, but access_count=2 triggers recheck
        decision = cache.get("critical_data", "source", None)
        assert decision.should_refetch is True
        assert "repeated read" in decision.reason.lower()
        assert decision.access_count == 2

    def test_entity_segmentation(self):
        policy = InvalidationPolicy(segmentation=ContextSegmentation.ENTITY)
        cache = ContextCache(policy)

        cache.add("profile", {"user": "alice"}, "fetch", "alice")
        cache.add("profile", {"user": "bob"}, "fetch", "bob")

        # Alice's profile should not leak to Bob
        decision_alice = cache.get("profile", "fetch", "alice")
        assert decision_alice.value == {"user": "alice"}

        decision_bob = cache.get("profile", "fetch", "bob")
        assert decision_bob.value == {"user": "bob"}

        # Accessing with wrong entity should miss
        decision_wrong = cache.get("profile", "fetch", "charlie")
        assert decision_wrong.should_refetch is True

    def test_source_segmentation(self):
        policy = InvalidationPolicy(segmentation=ContextSegmentation.SOURCE)
        cache = ContextCache(policy)

        cache.add("result", {"source": "api1"}, "fetch_api1", None)
        cache.add("result", {"source": "api2"}, "fetch_api2", None)

        # Data from api1 should not leak to api2
        decision_api1 = cache.get("result", "fetch_api1", None)
        assert decision_api1.value == {"source": "api1"}

        decision_api2 = cache.get("result", "fetch_api2", None)
        assert decision_api2.value == {"source": "api2"}

    def test_both_segmentation(self):
        policy = InvalidationPolicy(segmentation=ContextSegmentation.BOTH)
        cache = ContextCache(policy)

        cache.add("data", {"v": 1}, "source_x", "alice")
        cache.add("data", {"v": 2}, "source_y", "alice")
        cache.add("data", {"v": 3}, "source_x", "bob")

        assert cache.get("data", "source_x", "alice").value == {"v": 1}
        assert cache.get("data", "source_y", "alice").value == {"v": 2}
        assert cache.get("data", "source_x", "bob").value == {"v": 3}


class TestErrorHandling:
    """Test error invalidation and rate-limit detection."""

    def test_invalidate_on_error(self):
        policy = InvalidationPolicy()
        cache = ContextCache(policy)

        cache.add("data1", {"v": 1}, "tool", "alice")
        cache.add("data2", {"v": 2}, "tool", "alice")
        cache.add("data3", {"v": 3}, "other_tool", "alice")

        # Error on 'tool' should invalidate only data1 and data2
        error = Exception("Connection failed")
        is_rate_limit = cache.invalidate_on_error("tool", error, "alice")

        assert is_rate_limit is False
        assert cache.get("data1", "tool", "alice").should_refetch is True
        assert cache.get("data2", "tool", "alice").should_refetch is True
        # data3 should still be fresh (different source)
        assert cache.get("data3", "other_tool", "alice").should_refetch is False

    def test_rate_limit_detection(self):
        policy = InvalidationPolicy()
        cache = ContextCache(policy)

        cache.add("data", {"v": 1}, "api", None)

        # Rate-limit error should be detected
        rate_limit_error = Exception("Rate limit exceeded (429)")
        is_rate_limit = cache.invalidate_on_error("api", rate_limit_error, None)
        assert is_rate_limit is True

        # Regular error should not be detected as rate-limit
        regular_error = Exception("API key invalid")
        is_rate_limit = cache.invalidate_on_error("api", regular_error, None)
        assert is_rate_limit is False

    def test_custom_rate_limit_pattern(self):
        policy = InvalidationPolicy(rate_limit_patterns=[r"quota.*exceeded", r"too.*many"])
        cache = ContextCache(policy)

        error1 = Exception("quota exceeded for this user")
        assert cache._is_rate_limit_error(error1) is True

        error2 = Exception("too many requests in flight")
        assert cache._is_rate_limit_error(error2) is True

        error3 = Exception("authentication failed")
        assert cache._is_rate_limit_error(error3) is False


class TestVersioning:
    """Test immutable versioning and history."""

    def test_version_immutability(self):
        policy = InvalidationPolicy()
        cache = ContextCache(policy)

        v1 = cache.add("data", {"v": 1}, "source", None)
        v2 = cache.add("data", {"v": 2}, "source", None)

        history = cache.get_history("data", "source", None)
        assert len(history.versions) == 2
        assert history.versions[0].version_id == v1
        assert history.versions[1].version_id == v2
        assert history.current_version().version_id == v2

    def test_audit_trail(self):
        policy = InvalidationPolicy()
        cache = ContextCache(policy)

        cache.add("data", {"v": 1}, "source", None)
        cache.get("data", "source", None)
        cache.advance_step()
        cache.get("data", "source", None)

        audit = cache.get_audit_log()
        assert len(audit) > 0
        assert any(e["event_type"] == "add" for e in audit)
        assert any(e["event_type"] == "get_hit" for e in audit)
        assert any(e["event_type"] == "step_advanced" for e in audit)


class TestStateSnapshot:
    """Test cache state introspection."""

    def test_snapshot_shows_all_entries(self):
        policy = InvalidationPolicy()
        cache = ContextCache(policy)

        cache.add("user", {"id": "alice"}, "fetch_user", "alice", Criticality.HIGH)
        cache.add("docs", [1, 2, 3], "search", None, Criticality.LOW)
        cache.get("user", "fetch_user", "alice")
        cache.get("user", "fetch_user", "alice")

        snapshot = cache.get_state_snapshot()
        assert len(snapshot) == 2

        user_entry = snapshot.get("alice:fetch_user:user")
        assert user_entry is not None
        assert user_entry["value"] == {"id": "alice"}
        assert user_entry["criticality"] == "high"
        assert user_entry["access_count"] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
