"""Structured diagnostic types for ``mycelium doctor``."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class DoctorStatus(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


# How the check established its conclusion.
EVIDENCE_STATIC = "statically_verified"
EVIDENCE_RUNTIME = "runtime_registration_verified"
EVIDENCE_CONNECTIVITY = "connectivity_verified"
EVIDENCE_OPERATOR = "operator_asserted"
EVIDENCE_NOT_VERIFIABLE = "not_verifiable"

EVIDENCE_KINDS = frozenset(
    {
        EVIDENCE_STATIC,
        EVIDENCE_RUNTIME,
        EVIDENCE_CONNECTIVITY,
        EVIDENCE_OPERATOR,
        EVIDENCE_NOT_VERIFIABLE,
    }
)


@dataclass(frozen=True)
class DoctorCheck:
    id: str
    category: str
    status: DoctorStatus
    summary: str
    details: str = ""
    remediation: str = ""
    evidence: str = EVIDENCE_STATIC
    blocking: bool = True

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        return payload


@dataclass
class DoctorReport:
    overall_status: DoctorStatus
    profile: str
    checks: list[DoctorCheck] = field(default_factory=list)
    pass_count: int = 0
    warning_count: int = 0
    failure_count: int = 0
    skipped_count: int = 0
    production_ready: bool = False
    distributed_ready: bool = False
    load_error: str | None = None
    protection_coverage: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status.value,
            "profile": self.profile,
            "checks": [check.to_dict() for check in self.checks],
            "pass_count": self.pass_count,
            "warning_count": self.warning_count,
            "failure_count": self.failure_count,
            "skipped_count": self.skipped_count,
            "production_ready": self.production_ready,
            "distributed_ready": self.distributed_ready,
            "load_error": self.load_error,
            "protection_coverage": self.protection_coverage,
        }


__all__ = [
    "EVIDENCE_CONNECTIVITY",
    "EVIDENCE_KINDS",
    "EVIDENCE_NOT_VERIFIABLE",
    "EVIDENCE_OPERATOR",
    "EVIDENCE_RUNTIME",
    "EVIDENCE_STATIC",
    "DoctorCheck",
    "DoctorReport",
    "DoctorStatus",
]
