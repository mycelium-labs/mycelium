"""Human and JSON rendering for verify reports."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TextIO

from mycelium.verify.types import VerificationReport, VerificationStatus

_SCENARIO_LABELS = {
    "redispatch": "Redispatch",
    "contention": "Contention",
    "storage-outage": "Storage outage",
    "worker-crash": "Worker crash",
    "ambiguous-effect": "Ambiguous effect",
    "reconcile": "Reconcile",
    "secret-in-args": "Secret-in-args",
    "entity-guard": "Entity-guard",
    "destructive-confirm": "Destructive-confirm",
    "authority-window": "Authority-window",
    "use-time-currency": "Use-time-currency",
    "trust-boundary": "Trust boundary",
}


def render_json(report: VerificationReport) -> str:
    from mycelium.secret_protection import sanitize_secrets

    return json.dumps(
        sanitize_secrets(report.to_dict(), entropy_detection=False),
        indent=2,
        sort_keys=True,
    )


def render_human(report: VerificationReport) -> str:
    doctor_status = "ERROR"
    if report.doctor:
        doctor_status = str(report.doctor.get("overall_status") or "ERROR")
    lines = [
        "Mycelium Verify",
        "",
        f"Doctor:             {doctor_status}",
        f"Backend:            {report.backend}",
        f"Topology:           {report.topology or 'unspecified'}",
        f"Isolation:          {report.isolation_status.value}",
        "",
    ]
    if report.isolation_detail and report.isolation_status != VerificationStatus.PASS:
        lines.append(f"         → {report.isolation_detail}")
        lines.append("")
    if report.framework_error:
        lines.append(f"Framework error:    {report.framework_error}")
        lines.append("")
    for artifact in report.artifacts:
        if Path(artifact).exists():
            lines.append(f"Retained artifact:  {artifact}")
    if report.artifacts:
        lines.append("")
    for item in report.scenarios:
        label = _SCENARIO_LABELS.get(item.scenario, item.scenario)
        pad = f"{label:<16}"
        line = f"[{item.status.value}] {pad} {item.summary}"
        if item.status != VerificationStatus.PASS:
            extra = item.observed_behavior or item.limitations
            if item.remediation:
                line += f"\n         → {item.remediation}"
            elif extra:
                detail = extra if isinstance(extra, str) else "; ".join(extra)
                line += f"\n         → {detail}"
        retained = [artifact for artifact in item.artifacts if Path(artifact).exists()]
        for artifact in retained:
            line += f"\n         artifact: {artifact}"
        lines.append(line)
    if not report.scenarios and report.refused:
        lines.append("[ERROR] Isolation         verification refused")
        if report.isolation_detail:
            lines.append(f"         → {report.isolation_detail}")
    lines.append("")
    lines.append(
        f"Production ready:       {'YES' if report.production_ready else 'NO'}"
    )
    lines.append(
        f"Empirically verified:   {'YES' if report.empirically_verified else 'NO'}"
    )
    return "\n".join(lines) + "\n"


def write_report(
    report: VerificationReport,
    *,
    as_json: bool = False,
    stream: TextIO,
) -> None:
    if as_json:
        stream.write(render_json(report) + "\n")
    else:
        stream.write(render_human(report))


def write_diagnostic(message: str) -> None:
    print(message, file=sys.stderr)


__all__ = ["render_human", "render_json", "write_diagnostic", "write_report"]
