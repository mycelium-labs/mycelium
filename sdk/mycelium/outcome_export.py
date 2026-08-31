"""Export the outcome stream without turning Mycelium into a dashboard.

The classes in this module are small ``OutcomeStorage`` sinks.  Applications
keep ownership of their OpenTelemetry provider, Prometheus scrape endpoint,
and webhook receiver; Mycelium only translates its existing outcome rows into
a stable, low-cardinality metric contract.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen

from mycelium.outcome_emit import (
    EVENT_DECISION_DENIAL,
    EVENT_FENCE_REJECTION,
    EVENT_LEASE_RENEWAL_FAILURE,
    EVENT_RELEASE,
    GATE_HARD_BLOCK,
    OutcomeRow,
    OutcomeStorage,
)

METRIC_HARD_BLOCKS = "mycelium.hard_blocks"
METRIC_AMBIGUITY_AGE = "mycelium.ambiguity.age"
METRIC_FENCE_REJECTIONS = "mycelium.fence_rejections"
METRIC_LEASE_RENEWAL_FAILURES = "mycelium.lease_renewal_failures"
METRIC_DECISION_DENIALS = "mycelium.decision_denials"
METRIC_RECOVERY_TIME = "mycelium.recovery.time"
METRIC_OPERATOR_RELEASES = "mycelium.operator_releases"

COUNTER = "counter"
HISTOGRAM = "histogram"

METRIC_DEFINITIONS: Mapping[str, tuple[str, str, str]] = {
    METRIC_HARD_BLOCKS: (COUNTER, "Hard-blocked transition resolutions", "1"),
    METRIC_AMBIGUITY_AGE: (
        HISTOGRAM,
        "Age of an unresolved ambiguous transition when observed",
        "s",
    ),
    METRIC_FENCE_REJECTIONS: (
        COUNTER,
        "Writes refused because a transition owner or fence was superseded",
        "1",
    ),
    METRIC_LEASE_RENEWAL_FAILURES: (
        COUNTER,
        "Execution lease renewal attempts that failed",
        "1",
    ),
    METRIC_DECISION_DENIALS: (
        COUNTER,
        "Final-boundary decisions that denied execution",
        "1",
    ),
    METRIC_RECOVERY_TIME: (
        HISTOGRAM,
        "Time from first observed ambiguity to release or safe resolution",
        "s",
    ),
    METRIC_OPERATOR_RELEASES: (
        COUNTER,
        "Recorded operator releases",
        "1",
    ),
}

_AMBIGUOUS_OUTCOMES = frozenset(
    {"UNKNOWN", "FAILED_AFTER_EFFECT", "BLOCKED", "EXPIRED"}
)


@dataclass(frozen=True)
class OutcomeMetricPoint:
    """One backend-neutral counter increment or histogram observation."""

    name: str
    kind: str
    value: float
    attributes: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "value": self.value,
            "unit": METRIC_DEFINITIONS[self.name][2],
            "attributes": dict(self.attributes),
        }


class OutcomeMetricProjector:
    """Statefully project outcome rows into the standard metric contract.

    Mycelium remains resolution-event driven: ambiguity age is sampled when an
    ambiguous row is emitted, not by a polling loop. Recovery time is emitted
    once, when a later release or non-ambiguous resolution closes that period.
    """

    def __init__(self) -> None:
        self._ambiguous_since: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def attributes(row: OutcomeRow) -> dict[str, str]:
        # Deliberately omit request_id, run_id, owner, and free-form reasons:
        # those are useful event fields but unsafe metric dimensions.
        return {
            "agent_id": row.agent_id,
            "tool": row.tool,
            "terminal_outcome": row.terminal_outcome or "",
        }

    def project(self, row: OutcomeRow) -> tuple[OutcomeMetricPoint, ...]:
        attrs = self.attributes(row)
        points: list[OutcomeMetricPoint] = []

        def counter(name: str) -> None:
            points.append(OutcomeMetricPoint(name, COUNTER, 1.0, attrs))

        if (row.gate or "").upper() == GATE_HARD_BLOCK:
            counter(METRIC_HARD_BLOCKS)
        if row.event == EVENT_FENCE_REJECTION:
            counter(METRIC_FENCE_REJECTIONS)
        if row.event == EVENT_LEASE_RENEWAL_FAILURE:
            counter(METRIC_LEASE_RENEWAL_FAILURES)
        if row.event == EVENT_DECISION_DENIAL:
            counter(METRIC_DECISION_DENIALS)
        if row.event == EVENT_RELEASE:
            counter(METRIC_OPERATOR_RELEASES)

        ambiguous = (row.terminal_outcome or "").upper() in _AMBIGUOUS_OUTCOMES
        with self._lock:
            started = self._ambiguous_since.get(row.request_id)
            recovered = started is not None and (
                row.event == EVENT_RELEASE
                or (not ambiguous and (row.gate or "").upper() in {"ALLOW", "RETURN"})
                or (not ambiguous and (row.terminal_outcome or "").upper()
                    in {"COMPLETED", "FAILED_BEFORE_EFFECT", "IN_FLIGHT"})
            )
            if recovered:
                points.append(
                    OutcomeMetricPoint(
                        METRIC_RECOVERY_TIME,
                        HISTOGRAM,
                        max(0.0, row.ts - started),
                        attrs,
                    )
                )
                del self._ambiguous_since[row.request_id]
            elif ambiguous and row.event != EVENT_RELEASE:
                if started is None:
                    started = row.ts
                    self._ambiguous_since[row.request_id] = started
                points.append(
                    OutcomeMetricPoint(
                        METRIC_AMBIGUITY_AGE,
                        HISTOGRAM,
                        max(0.0, row.ts - started),
                        attrs,
                    )
                )
        return tuple(points)


class FanoutOutcomeStorage(OutcomeStorage):
    """Append every row to a primary store and zero or more export sinks.

    ``list_all`` reads only the primary store, avoiding duplicate rows when
    exporters are combined with a durable NDJSON/Postgres/Redis backend.
    Every sink is attempted before the first failure is re-raised.
    """

    def __init__(self, primary: OutcomeStorage, *sinks: OutcomeStorage) -> None:
        self._primary = primary
        self._sinks = (primary, *sinks)

    def append(self, row: OutcomeRow) -> None:
        first_error: Exception | None = None
        for sink in self._sinks:
            try:
                sink.append(row)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def list_all(self) -> list[OutcomeRow]:
        return self._primary.list_all()


class OpenTelemetryOutcomeStorage(OutcomeStorage):
    """Record standard outcome metrics through an application-owned OTel meter."""

    def __init__(self, meter: Any = None) -> None:
        if meter is None:
            try:
                from opentelemetry import metrics
            except ImportError as exc:  # pragma: no cover - environment dependent
                raise ImportError(
                    "OpenTelemetry outcome export requires opentelemetry-api; "
                    "install mycelium[opentelemetry]"
                ) from exc
            meter = metrics.get_meter("mycelium.outcomes")
        self._projector = OutcomeMetricProjector()
        self._instruments: dict[str, Any] = {}
        for name, (kind, description, unit) in METRIC_DEFINITIONS.items():
            if kind == COUNTER:
                instrument = meter.create_counter(name, description=description, unit=unit)
            else:
                instrument = meter.create_histogram(name, description=description, unit=unit)
            self._instruments[name] = instrument

    def append(self, row: OutcomeRow) -> None:
        for point in self._projector.project(row):
            instrument = self._instruments[point.name]
            if point.kind == COUNTER:
                instrument.add(point.value, attributes=point.attributes)
            else:
                instrument.record(point.value, attributes=point.attributes)

    def list_all(self) -> list[OutcomeRow]:
        return []


class PrometheusOutcomeStorage(OutcomeStorage):
    """Record standard outcome metrics in a prometheus-client registry."""

    _LABELS = ("agent_id", "tool", "terminal_outcome")

    def __init__(self, registry: Any = None) -> None:
        try:
            from prometheus_client import REGISTRY, Counter, Histogram
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ImportError(
                "Prometheus outcome export requires prometheus-client; "
                "install mycelium[prometheus]"
            ) from exc
        if registry is None:
            registry = REGISTRY
        self._projector = OutcomeMetricProjector()
        self._instruments: dict[str, Any] = {}
        for dotted_name, (kind, description, _unit) in METRIC_DEFINITIONS.items():
            name = dotted_name.replace(".", "_")
            if kind == COUNTER:
                instrument = Counter(name, description, self._LABELS, registry=registry)
            else:
                instrument = Histogram(
                    name + "_seconds",
                    description,
                    self._LABELS,
                    registry=registry,
                )
            self._instruments[dotted_name] = instrument

    def append(self, row: OutcomeRow) -> None:
        for point in self._projector.project(row):
            label_values = [point.attributes[label] for label in self._LABELS]
            instrument = self._instruments[point.name].labels(*label_values)
            if point.kind == COUNTER:
                instrument.inc(point.value)
            else:
                instrument.observe(point.value)

    def list_all(self) -> list[OutcomeRow]:
        return []


class WebhookOutcomeStorage(OutcomeStorage):
    """POST each outcome and its projected metrics to a generic HTTP endpoint."""

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        secret: str | bytes | None = None,
        timeout: float = 5.0,
    ) -> None:
        import math
        if not url:
            raise ValueError("webhook url must be non-empty")
        if isinstance(timeout, bool):
            raise TypeError("webhook timeout must not be a boolean")
        if not isinstance(timeout, (int, float)):
            raise TypeError("webhook timeout must be a number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("webhook timeout must be a positive finite number")
        self._url = url
        self._headers = dict(headers or {})
        self._secret = secret.encode() if isinstance(secret, str) else secret
        self._timeout = timeout
        self._projector = OutcomeMetricProjector()

    def append(self, row: OutcomeRow) -> None:
        payload = {
            "schema": "mycelium.outcome.v1",
            "id": row.event_id,
            "type": f"mycelium.outcome.{row.event}",
            "timestamp": datetime.fromtimestamp(row.ts, tz=timezone.utc).isoformat(),
            "outcome": row.to_dict(),
            "metrics": [point.to_dict() for point in self._projector.project(row)],
        }
        body = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "mycelium-outcome-webhook/1",
            **self._headers,
        }
        if row.event_id:
            headers["X-Mycelium-Event-ID"] = row.event_id
        if self._secret is not None:
            signature = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
            headers["X-Mycelium-Signature"] = f"sha256={signature}"
        request = Request(self._url, data=body, headers=headers, method="POST")
        with urlopen(request, timeout=self._timeout) as response:  # noqa: S310
            response.read(1)

    def list_all(self) -> list[OutcomeRow]:
        return []


def export_rows(rows: Sequence[OutcomeRow], *sinks: OutcomeStorage) -> None:
    """Replay stored outcome rows through one or more export sinks."""
    for row in sorted(rows, key=lambda item: item.ts):
        for sink in sinks:
            sink.append(row)


__all__ = [
    "FanoutOutcomeStorage",
    "METRIC_AMBIGUITY_AGE",
    "METRIC_DECISION_DENIALS",
    "METRIC_DEFINITIONS",
    "METRIC_FENCE_REJECTIONS",
    "METRIC_HARD_BLOCKS",
    "METRIC_LEASE_RENEWAL_FAILURES",
    "METRIC_OPERATOR_RELEASES",
    "METRIC_RECOVERY_TIME",
    "OpenTelemetryOutcomeStorage",
    "OutcomeMetricPoint",
    "OutcomeMetricProjector",
    "PrometheusOutcomeStorage",
    "WebhookOutcomeStorage",
    "export_rows",
]
