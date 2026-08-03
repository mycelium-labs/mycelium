"""Shared ASCII-safe narration for failure-case scripts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


def narrate(
    *,
    case_id: str,
    title: str,
    gate: str,
    without: str,
    with_guard: str,
    prove: Callable[[], dict[str, Any]],
) -> int:
    print(f"=== {case_id}: {title} ===")
    print(f"gate:     {gate}")
    print(f"without:  {without}")
    print(f"with:     {with_guard}")
    result = prove()
    print("proof:")
    for key, value in result.items():
        print(f"  {key}={value!r}")
    print(f"OK — {gate}")
    return 0
