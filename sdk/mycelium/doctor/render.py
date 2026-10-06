"""Human and JSON rendering for doctor reports."""

from __future__ import annotations

import json
from typing import TextIO

from mycelium.doctor.types import DoctorReport, DoctorStatus


def render_json(report: DoctorReport) -> str:
    """Stable machine-readable JSON for CI (no secrets)."""
    from mycelium.secret_protection import sanitize_secrets

    return json.dumps(
        sanitize_secrets(report.to_dict(), entropy_detection=False),
        indent=2,
        sort_keys=True,
    )


def render_human(report: DoctorReport, *, verbose: bool = False) -> str:
    from mycelium.secret_protection import sanitize_text

    lines = ["Mycelium Doctor", ""]
    # Collapse to one line per category for the headline view, but keep every
    # check when verbose or when status is WARN/FAIL.
    if verbose:
        for check in report.checks:
            lines.append(_format_check_line(check, verbose=True))
    else:
        # Show non-SKIP checks; prefer FAIL > WARN > PASS per category order.
        seen_categories: set[str] = set()
        headline: list[tuple[str, object]] = []
        for check in report.checks:
            if check.status == DoctorStatus.SKIP:
                continue
            key = check.category
            if key in seen_categories and check.status == DoctorStatus.PASS:
                continue
            if check.status in (DoctorStatus.FAIL, DoctorStatus.WARN):
                headline.append((key, check))
                seen_categories.add(key)
            elif key not in seen_categories:
                headline.append((key, check))
                seen_categories.add(key)
        for _key, check in headline:
            lines.append(_format_check_line(check, verbose=False))

    if report.protection_coverage:
        lines.extend(["", "Tool protection coverage (provider boundaries remain unverified)"])
        for row in report.protection_coverage:
            lines.append(
                sanitize_text(
                    f"  {row['tool']}: {row['wrapper_status']}; "
                    f"ledger={row['ledger_storage'] or 'unconfigured'}; "
                    f"identity={row['request_identity']}"
                )
            )

    lines.append("")
    lines.append(f"Production ready: {'YES' if report.production_ready else 'NO'}")
    lines.append(
        f"Distributed ready: {'YES' if report.distributed_ready else 'NO'}"
    )
    if report.load_error:
        lines.append(f"Load error: {report.load_error}")
    return "\n".join(lines) + "\n"


def _format_check_line(check: object, *, verbose: bool) -> str:
    from mycelium.doctor.types import DoctorCheck
    from mycelium.secret_protection import sanitize_text

    assert isinstance(check, DoctorCheck)
    pad_cat = f"{check.category:<20}"
    line = f"[{check.status.value}] {pad_cat} {sanitize_text(check.summary)}"
    if check.status in (DoctorStatus.WARN, DoctorStatus.FAIL) and check.remediation:
        line += f"\n         → {check.remediation}"
    if verbose:
        extras = []
        if check.details:
            extras.append(check.details)
        extras.append(f"evidence={check.evidence}")
        extras.append(f"id={check.id}")
        line += "\n         " + " | ".join(extras)
    return line


def write_report(
    report: DoctorReport,
    *,
    as_json: bool = False,
    verbose: bool = False,
    stream: TextIO,
) -> None:
    if as_json:
        stream.write(render_json(report) + "\n")
    else:
        stream.write(render_human(report, verbose=verbose))


__all__ = ["render_human", "render_json", "write_report"]
