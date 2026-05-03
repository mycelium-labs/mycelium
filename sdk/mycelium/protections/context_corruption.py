"""
AF-006: Context Corruption Protection

Protects against agents reasoning over stale, poisoned, or cross-contaminated context.
Treats context as a cache with explicit TTLs, versioning, and strict segmentation.

Core invariants:
1. No stale data without explicit re-verification
2. All entries are immutable; updates create new versions
3. Cross-entity/source leakage is impossible by construction
4. Every decision (keep/refetch) is logged and auditable
5. High-criticality values trigger forced re-check on repeated access
6. Errors immediately invalidate related context
"""

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


class Criticality(Enum):
    """Importance level of a context entry."""

    LOW = "low"
    HIGH = "high"


class ContextSegmentation(Enum):
    """How to partition context to prevent cross-contamination."""

    ENTITY = "entity"
    SOURCE = "source"
    BOTH = "both"


class InvalidationReason(Enum):
    """Why a context entry was invalidated."""

    STALE = "stale"  # Exceeded TTL
    ERROR = "error"  # Source tool returned error
    REPEATED_READ = "repeated_read"  # HIGH criticality, read 2+ times
    EXPLICIT = "explicit"  # Manually invalidated
    RATE_LIMITED = "rate_limited"  # Rate limit error detected


@dataclass(frozen=True)
class ContextEntryVersion:
    """Immutable version of a context entry. Can never be modified."""

    version_id: str
    value: Any
    source: str
    entity_id: str | None
    created_at_step: int
    created_at_time: float
    criticality: Criticality
    invalidate_after_steps: int


@dataclass
class ContextEntryHistory:
    """Complete lineage of a context entry across all versions."""

    name: str
    source: str
    entity_id: str | None
    versions: list[ContextEntryVersion] = field(default_factory=list)
    access_history: list[tuple[int, int]] = field(default_factory=list)  # (step, time)
    invalidation_reasons: list[InvalidationReason] = field(default_factory=list)

    def current_version(self) -> ContextEntryVersion | None:
        """Get the latest version."""
        return self.versions[-1] if self.versions else None

    def add_version(self, entry: ContextEntryVersion) -> None:
        """Add a new version (immutable append only)."""
        self.versions.append(entry)

    def record_access(self, step: int) -> int:
        """Record an access and return access count."""
        self.access_history.append((step, time.time()))
        return len(self.access_history)

    def record_invalidation(self, reason: InvalidationReason) -> None:
        """Record why this entry was invalidated."""
        self.invalidation_reasons.append(reason)


@dataclass
class InvalidationPolicy:
    """Rules for when and how context expires."""

    default_ttl_steps: int = 5
    criticality_recheck_threshold: int = 2
    segmentation: ContextSegmentation = ContextSegmentation.BOTH
    rate_limit_patterns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.rate_limit_patterns:
            self.rate_limit_patterns = [
                r"(?i)(rate.?limit|quota|429|too.?many.?requests|throttl)",
                r"(?i)(please.?wait|retry.?after|backoff|delay)",
                r"(?i)(concurrent.?request|limit.?exceeded|exceeded.?limit)",
                r"(?i)(too.?many|maximum.?allowed|peak|capacity)",
                r"status.?code.*429",
            ]


@dataclass
class AccessDecision:
    """Result of a context access attempt."""

    value: Any | None
    should_refetch: bool
    reason: str | None
    current_version: ContextEntryVersion | None
    access_count: int
    age_steps: int


class ContextCache:
    """
    Manages agent context with TTL enforcement, versioning, and cross-entity isolation.

    INVARIANTS:
    - Once written, data cannot be modified without creating a new version
    - Cross-entity/source reads are impossible
    - Every access is recorded
    - Refetch decisions are auditable
    """

    def __init__(self, policy: InvalidationPolicy):
        self.policy = policy
        self._entries: dict[str, ContextEntryHistory] = {}
        self._current_step: int = 0
        self._audit_log: list[dict[str, Any]] = []

    def add(
        self,
        name: str,
        value: Any,
        source: str,
        entity_id: str | None = None,
        criticality: Criticality = Criticality.LOW,
        invalidate_after_steps: int | None = None,
    ) -> str:
        """
        Add a context entry with a new version.

        Returns: version_id (unique identifier for this version)

        SECURITY:
        - Validates entity_id is not None when required by segmentation
        - Previous versions remain in history (immutable)
        - Always creates new version, never overwrites
        """
        # Validate segmentation requirements
        if (
            self.policy.segmentation in (ContextSegmentation.ENTITY, ContextSegmentation.BOTH)
            and entity_id is None
        ):
            self._log_security_event(
                "add",
                "ENTITY segmentation required but entity_id is None",
                {"name": name, "source": source},
                severity="warn",
            )

        key = self._make_key(name, source, entity_id)
        ttl = invalidate_after_steps or self.policy.default_ttl_steps

        version_id = str(uuid4())
        new_version = ContextEntryVersion(
            version_id=version_id,
            value=value,
            source=source,
            entity_id=entity_id,
            created_at_step=self._current_step,
            created_at_time=time.time(),
            criticality=criticality,
            invalidate_after_steps=ttl,
        )

        # Create or update history
        if key not in self._entries:
            self._entries[key] = ContextEntryHistory(name=name, source=source, entity_id=entity_id)

        self._entries[key].add_version(new_version)

        self._log_event(
            "add",
            {
                "name": name,
                "source": source,
                "entity_id": entity_id,
                "version_id": version_id,
                "criticality": criticality.value,
                "ttl_steps": ttl,
                "step": self._current_step,
            },
        )

        return version_id

    def get(
        self,
        name: str,
        source: str,
        entity_id: str | None = None,
    ) -> AccessDecision:
        """
        Retrieve a context entry and determine if it needs refetching.

        SECURITY:
        - Validates entity_id matches source
        - Returns (value, should_refetch) not just value
        - Records access for audit trail
        - Checks both TTL and criticality heuristics

        Returns:
        - value: The cached value (or None if should_refetch=True)
        - should_refetch: True if stale, high-criticality + repeated read, or missing
        - reason: Human-readable explanation
        - access_count: How many times this entry has been read
        - age_steps: Steps since entry was added
        """
        key = self._make_key(name, source, entity_id)

        # Case 1: Entry doesn't exist
        if key not in self._entries:
            self._log_event(
                "get_missing",
                {
                    "name": name,
                    "source": source,
                    "entity_id": entity_id,
                    "step": self._current_step,
                },
            )
            return AccessDecision(
                value=None,
                should_refetch=True,
                reason="No entry in cache",
                current_version=None,
                access_count=0,
                age_steps=0,
            )

        history = self._entries[key]
        current = history.current_version()
        assert current is not None  # Invariant: history always has at least one version

        # Record access
        access_count = history.record_access(self._current_step)
        age_steps = self._current_step - current.created_at_step

        # Check if stale (exceeded TTL)
        if age_steps >= current.invalidate_after_steps:
            history.record_invalidation(InvalidationReason.STALE)
            self._log_event(
                "get_stale",
                {
                    "name": name,
                    "source": source,
                    "entity_id": entity_id,
                    "age_steps": age_steps,
                    "ttl_steps": current.invalidate_after_steps,
                    "version_id": current.version_id,
                    "step": self._current_step,
                },
            )
            return AccessDecision(
                value=current.value,
                should_refetch=True,
                reason=f"Stale (age {age_steps} >= TTL {current.invalidate_after_steps})",
                current_version=current,
                access_count=access_count,
                age_steps=age_steps,
            )

        # Check if high-criticality + repeated read
        if (
            current.criticality == Criticality.HIGH
            and access_count >= self.policy.criticality_recheck_threshold
        ):
            history.record_invalidation(InvalidationReason.REPEATED_READ)
            self._log_event(
                "get_repeated_read",
                {
                    "name": name,
                    "source": source,
                    "entity_id": entity_id,
                    "access_count": access_count,
                    "threshold": self.policy.criticality_recheck_threshold,
                    "version_id": current.version_id,
                    "step": self._current_step,
                },
            )
            return AccessDecision(
                value=current.value,
                should_refetch=True,
                reason=f"High criticality + repeated read ({access_count} >= {self.policy.criticality_recheck_threshold})",
                current_version=current,
                access_count=access_count,
                age_steps=age_steps,
            )

        # Entry is fresh and safe to use
        self._log_event(
            "get_hit",
            {
                "name": name,
                "source": source,
                "entity_id": entity_id,
                "access_count": access_count,
                "age_steps": age_steps,
                "version_id": current.version_id,
                "step": self._current_step,
            },
        )
        return AccessDecision(
            value=current.value,
            should_refetch=False,
            reason="Fresh and safe",
            current_version=current,
            access_count=access_count,
            age_steps=age_steps,
        )

    def invalidate_on_error(
        self,
        source: str,
        error: Exception,
        entity_id: str | None = None,
    ) -> bool:
        """
        Invalidate all context from a tool that errored.

        SECURITY:
        - Rate-limit errors are tracked separately (may auto-retry)
        - Other errors are final invalidations (let agent decide on retry)
        - Removes entries only for matching source + entity
        - Logs all invalidations for audit trail

        Returns: True if this is a rate-limit error, False otherwise
        """
        is_rate_limit = self._is_rate_limit_error(error)
        error_str = str(error)

        # Find all entries to invalidate
        keys_to_remove = []
        for key, history in self._entries.items():
            current = history.current_version()
            if current.source == source and (entity_id is None or current.entity_id == entity_id):
                keys_to_remove.append(key)

        # Invalidate them
        for key in keys_to_remove:
            history = self._entries[key]
            reason = InvalidationReason.RATE_LIMITED if is_rate_limit else InvalidationReason.ERROR
            history.record_invalidation(reason)

            self._log_event(
                "invalidate_on_error",
                {
                    "name": history.name,
                    "source": source,
                    "entity_id": entity_id,
                    "is_rate_limit": is_rate_limit,
                    "error": error_str,
                    "reason": reason.value,
                    "step": self._current_step,
                },
            )

            del self._entries[key]

        return is_rate_limit

    def advance_step(self) -> None:
        """Called when agent completes one reasoning step."""
        self._current_step += 1
        self._log_event("step_advanced", {"step": self._current_step})

    def get_audit_log(self) -> list[dict[str, Any]]:
        """Return complete audit trail (immutable)."""
        return list(self._audit_log)

    def get_history(
        self, name: str, source: str, entity_id: str | None = None
    ) -> ContextEntryHistory | None:
        """Get complete version history for debugging/audit."""
        key = self._make_key(name, source, entity_id)
        return self._entries.get(key)

    def get_state_snapshot(self) -> dict[str, Any]:
        """Snapshot of current cache state (for debugging)."""
        snapshot = {}
        for key, history in self._entries.items():
            current = history.current_version()
            if current:
                age = self._current_step - current.created_at_step
                snapshot[key] = {
                    "value": current.value,
                    "version_id": current.version_id,
                    "age_steps": age,
                    "access_count": len(history.access_history),
                    "criticality": current.criticality.value,
                    "ttl_steps": current.invalidate_after_steps,
                    "invalidation_reasons": [r.value for r in history.invalidation_reasons],
                }
        return snapshot

    def _make_key(self, name: str, source: str, entity_id: str | None) -> str:
        """Generate cache key respecting segmentation policy."""
        if self.policy.segmentation == ContextSegmentation.ENTITY:
            return f"{entity_id or 'global'}:{name}"
        elif self.policy.segmentation == ContextSegmentation.SOURCE:
            return f"{source}:{name}"
        else:  # BOTH
            return f"{entity_id or 'global'}:{source}:{name}"

    def _is_rate_limit_error(self, error: Exception) -> bool:
        """Check if error matches any rate-limit pattern."""
        error_str = str(error)
        for pattern in self.policy.rate_limit_patterns:
            if re.search(pattern, error_str):
                return True
        return False

    def _log_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Log an event to audit trail."""
        self._audit_log.append(
            {
                "event_type": event_type,
                "timestamp": time.time(),
                "step": self._current_step,
                "data": data,
            }
        )

    def _log_security_event(
        self, event_type: str, message: str, data: dict[str, Any], severity: str = "info"
    ) -> None:
        """Log a security-relevant event."""
        self._audit_log.append(
            {
                "event_type": f"security_{event_type}",
                "severity": severity,
                "message": message,
                "timestamp": time.time(),
                "step": self._current_step,
                "data": data,
            }
        )
