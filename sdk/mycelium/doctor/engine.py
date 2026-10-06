"""Read-only doctor engine — separate from CLI rendering."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mycelium.config import PROFILE_DEVELOPMENT, ConfigError, MyceliumConfig, load_config
from mycelium.doctor import checks as _builtin_checks
from mycelium.doctor.coverage import protection_coverage
from mycelium.doctor.registry import DoctorContext, iter_registered_checks
from mycelium.doctor.types import DoctorCheck, DoctorReport, DoctorStatus
from mycelium.storage._helpers import redact_secrets

# Ensure built-in checks are registered on import.
_builtin_checks.ensure_builtin_checks_registered()


def _tally(checks: list[DoctorCheck]) -> tuple[int, int, int, int]:
    passed = sum(1 for c in checks if c.status == DoctorStatus.PASS)
    warned = sum(1 for c in checks if c.status == DoctorStatus.WARN)
    failed = sum(1 for c in checks if c.status == DoctorStatus.FAIL)
    skipped = sum(1 for c in checks if c.status == DoctorStatus.SKIP)
    return passed, warned, failed, skipped


def _overall(checks: list[DoctorCheck]) -> DoctorStatus:
    if any(c.status == DoctorStatus.FAIL for c in checks):
        return DoctorStatus.FAIL
    if any(c.status == DoctorStatus.WARN for c in checks):
        return DoctorStatus.WARN
    if checks and all(c.status == DoctorStatus.SKIP for c in checks):
        return DoctorStatus.SKIP
    return DoctorStatus.PASS


def _distributed_ready(cfg: MyceliumConfig, checks: list[DoctorCheck]) -> bool:
    if any(c.status == DoctorStatus.FAIL and c.id.startswith("topology") for c in checks):
        return False
    if any(
        c.id == "topology.omitted" and c.status == DoctorStatus.WARN for c in checks
    ):
        return False
    topology = (cfg.deployment or {}).get("topology")
    if topology != "multi_node":
        return False
    if any(c.status == DoctorStatus.FAIL for c in checks):
        return False
    return True


def _production_ready(cfg: MyceliumConfig, checks: list[DoctorCheck]) -> bool:
    if any(c.status == DoctorStatus.FAIL for c in checks):
        return False
    return cfg.profile == "production"


def run_doctor_on_config(
    config: MyceliumConfig,
    *,
    connectivity: bool = True,
    timeout_seconds: float = 2.0,
    verbose: bool = False,
    observed_tools: Mapping[str, Callable[..., Any]] | None = None,
) -> DoctorReport:
    """Run all registered checks against an already-loaded config."""
    ctx = DoctorContext(
        config=config,
        connectivity=connectivity,
        timeout_seconds=timeout_seconds,
        verbose=verbose,
    )
    checks: list[DoctorCheck] = []
    for _check_id, fn in iter_registered_checks():
        for item in fn(ctx):
            checks.append(item)

    passed, warned, failed, skipped = _tally(checks)
    return DoctorReport(
        overall_status=_overall(checks),
        profile=config.profile,
        checks=checks,
        pass_count=passed,
        warning_count=warned,
        failure_count=failed,
        skipped_count=skipped,
        production_ready=_production_ready(config, checks),
        distributed_ready=_distributed_ready(config, checks),
        protection_coverage=protection_coverage(config, observed_tools),
    )


def run_doctor(
    config_path: str | Path,
    *,
    connectivity: bool = True,
    timeout_seconds: float = 2.0,
    verbose: bool = False,
) -> DoctorReport:
    """Load YAML and run doctor checks. Never executes tools or LLM calls."""
    path = Path(config_path)
    try:
        config = load_config(path)
    except ConfigError as exc:
        message = redact_secrets(str(exc))
        check = DoctorCheck(
            id="configuration.load",
            category="Configuration",
            status=DoctorStatus.FAIL,
            summary="Configuration could not be loaded",
            details=message,
            remediation="Fix the YAML / environment until load_config succeeds.",
            evidence="statically_verified",
        )
        return DoctorReport(
            overall_status=DoctorStatus.FAIL,
            profile=PROFILE_DEVELOPMENT,
            checks=[check],
            pass_count=0,
            warning_count=0,
            failure_count=1,
            skipped_count=0,
            production_ready=False,
            distributed_ready=False,
            load_error=message,
        )
    except Exception as exc:  # pragma: no cover - unexpected I/O
        message = redact_secrets(str(exc))
        check = DoctorCheck(
            id="configuration.load",
            category="Configuration",
            status=DoctorStatus.FAIL,
            summary="Doctor failed while loading configuration",
            details=message,
            remediation="Inspect the config path and environment.",
            evidence="statically_verified",
        )
        return DoctorReport(
            overall_status=DoctorStatus.FAIL,
            profile=PROFILE_DEVELOPMENT,
            checks=[check],
            failure_count=1,
            load_error=message,
        )

    return run_doctor_on_config(
        config,
        connectivity=connectivity,
        timeout_seconds=timeout_seconds,
        verbose=verbose,
    )


def exit_code_for_report(
    report: DoctorReport,
    *,
    strict: bool = False,
) -> int:
    """Map a report to process exit codes (0 / 1 / 2)."""
    if report.load_error is not None:
        return 2
    if report.failure_count > 0:
        return 1
    if strict and report.warning_count > 0:
        return 1
    return 0


def doctor_options_from_mapping(raw: dict[str, Any]) -> dict[str, Any]:
    """Reserved for future doctor: YAML overrides (unused today)."""
    return dict(raw)


__all__ = [
    "exit_code_for_report",
    "run_doctor",
    "run_doctor_on_config",
]
