"""Python startup hook installed temporarily by ``mycelium run``."""

from __future__ import annotations

import os
import sys

try:
    from mycelium.auto_instrumentation import initialize_from_environment

    initialize_from_environment()
except BaseException as exc:
    print(
        f"mycelium: auto-instrumentation failed: {exc}",
        file=sys.stderr,
        flush=True,
    )
    os._exit(78)
