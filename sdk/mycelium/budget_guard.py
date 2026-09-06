"""BudgetGuard: run-level cost / token / wall-clock / step ceilings.

Refuse the next LLM or tool step when a host-declared ceiling would be
crossed. Never kills mid-step (avoids ambiguous UNKNOWN). Tokens and USD are
host-reported; Mycelium never prices models.

Not AF-010 yet — ship as ``budget:`` / ``@budget_guard``.
"""

from __future__ import annotations

import functools
import inspect
import json
import math
import re
import threading
import time
import warnings
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ParamSpec, TypeVar

from mycelium.action_ledger import (
    LedgerAlreadyResolvedError,
    LedgerHardBlockError,
    LedgerReleaseRefusedError,
)
from mycelium.loop_guard import (
    VERIFIED_ABORT_RUN,
    VERIFIED_ALLOW_ONCE,
    VERIFIED_CLEAR,
    VERIFIED_RESOLUTIONS,
    resolve_loop_scope_key,
    resolve_run_id,
)
from mycelium.storage.json_file import LockedJsonDictFile

P = ParamSpec("P")
R = TypeVar("R")
T = TypeVar("T")

KIND_LLM = "llm"
KIND_TOOL = "tool"
STEP_KINDS = frozenset({KIND_LLM, KIND_TOOL})

ON_MISSING_HARD = "hard"
ON_MISSING_WARN = "warn"
ON_MISSING_OFF = "off"
ON_MISSING_METER_MODES = frozenset(
    {ON_MISSING_HARD, ON_MISSING_WARN, ON_MISSING_OFF}
)

MISSING_USAGE_POLICY_WARN = "warn"
MISSING_USAGE_POLICY_ERROR = "error"
MISSING_USAGE_POLICIES = frozenset(
    {MISSING_USAGE_POLICY_WARN, MISSING_USAGE_POLICY_ERROR}
)

VIOLATION_BUDGET = "budget_exceeded"
VIOLATION_BUDGET_WARN = "budget_warning"
VIOLATION_MISSING_METER = "budget_missing_meter"
VIOLATION_MISSING_USAGE = "budget_missing_usage"

_DURATION_RE = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h|d)?\s*$",
    re.IGNORECASE,
)

_SCOPE_MISSING_WARNED = False
_MISSING_METER_WARNED: set[str] = set()
_MISSING_USAGE_WARNED: set[str] = set()


class BudgetAccountingError(Exception):
    """Raised when an LLM turn has no measurable usage under ``error`` policy.

    Subsequent LLM calls for the same ``run_id`` are blocked. Token counts
    are never invented. Provider exceptions are not replaced by this error.
    """

    def __init__(self, message: str, *, scope_key: str) -> None:
        super().__init__(message)
        self.scope_key = scope_key


def parse_duration_seconds(value: Any) -> float:
    """Parse ``15m`` / ``1h`` / ``30s`` / bare seconds into float seconds."""
    if isinstance(value, bool):
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, (int, float)):
        if float(value) <= 0:
            raise ValueError(f"duration must be positive, got {value!r}")
        return float(value)
    if not isinstance(value, str):
        raise ValueError(f"invalid duration: {value!r}")
    match = _DURATION_RE.match(value)
    if match is None:
        raise ValueError(
            f"invalid duration {value!r}; use seconds or a unit suffix "
            "(ms, s, m, h, d)"
        )
    amount = float(match.group("value"))
    if amount <= 0:
        raise ValueError(f"duration must be positive, got {value!r}")
    unit = (match.group("unit") or "s").lower()
    scale = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[unit]
    return amount * scale


@dataclass
class BudgetCeilings:
    """Host-declared ceilings (None = that meter disabled)."""

    max_duration: float | None = None
    max_steps: int | None = None
    max_tokens: int | None = None
    max_usd: float | None = None

    def __post_init__(self) -> None:
        if (
            self.max_duration is None
            and self.max_steps is None
            and self.max_tokens is None
            and self.max_usd is None
        ):
            raise ValueError(
                "budget requires at least one of max_duration, max_steps, "
                "max_tokens, max_usd"
            )
        if self.max_duration is not None:
            if isinstance(self.max_duration, bool):
                raise ValueError("max_duration must be a number, not a boolean")
            if not math.isfinite(self.max_duration):
                raise ValueError("max_duration must be a finite number")
            if self.max_duration <= 0:
                raise ValueError("max_duration must be positive")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be >= 1")
        if self.max_usd is not None:
            if isinstance(self.max_usd, bool):
                raise ValueError("max_usd must be a number, not a boolean")
            if not math.isfinite(self.max_usd):
                raise ValueError("max_usd must be a finite number")
            if self.max_usd <= 0:
                raise ValueError("max_usd must be positive")

    def requires_usage_meter(self) -> bool:
        return self.max_tokens is not None or self.max_usd is not None


@dataclass
class RemainingBudget:
    """Deterministic remaining budget snapshot (pitch: runway)."""

    duration_seconds: float | None
    steps: int | None
    tokens: int | None
    usd: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": self.duration_seconds,
            "steps": self.steps,
            "tokens": self.tokens,
            "usd": self.usd,
        }


@dataclass
class BudgetRunState:
    """Durable per-run budget meters and block state."""

    scope_key: str
    started_at: float = field(default_factory=time.time)
    steps: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    hard_blocked: bool = False
    soft_issued: dict[str, bool] = field(default_factory=dict)
    allow_once: bool = False
    blocked_dimension: str | None = None
    operator_resolution: str | None = None
    resolved_by: str | None = None
    reason: str | None = None
    resolved_at: float | None = None
    usage_unknown: bool = False
    last_model: str | None = None
    last_provider: str | None = None
    updated_at: float = field(default_factory=time.time)
    # ``None`` means no check has run yet; otherwise this records whether the
    # most recent check reserved a step for automatic accounting.
    last_check_incremented_steps: bool | None = None

    @property
    def tokens(self) -> int:
        return int(self.tokens_in) + int(self.tokens_out)

    @property
    def has_recorded_usage(self) -> bool:
        return self.tokens > 0 or self.usd > 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope_key": self.scope_key,
            "started_at": self.started_at,
            "steps": self.steps,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "usd": self.usd,
            "hard_blocked": self.hard_blocked,
            "soft_issued": dict(self.soft_issued),
            "allow_once": self.allow_once,
            "blocked_dimension": self.blocked_dimension,
            "operator_resolution": self.operator_resolution,
            "resolved_by": self.resolved_by,
            "reason": self.reason,
            "resolved_at": self.resolved_at,
            "usage_unknown": self.usage_unknown,
            "last_model": self.last_model,
            "last_provider": self.last_provider,
            "updated_at": self.updated_at,
            "last_check_incremented_steps": self.last_check_incremented_steps,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BudgetRunState:
        soft_raw = data.get("soft_issued") or {}
        return cls(
            scope_key=str(data["scope_key"]),
            started_at=float(data.get("started_at") or time.time()),
            steps=int(data.get("steps") or 0),
            tokens_in=int(data.get("tokens_in") or 0),
            tokens_out=int(data.get("tokens_out") or 0),
            usd=float(data.get("usd") or 0.0),
            hard_blocked=bool(data.get("hard_blocked")),
            soft_issued={str(k): bool(v) for k, v in soft_raw.items()},
            allow_once=bool(data.get("allow_once")),
            blocked_dimension=(
                str(data["blocked_dimension"])
                if data.get("blocked_dimension") is not None
                else None
            ),
            operator_resolution=data.get("operator_resolution"),
            resolved_by=data.get("resolved_by"),
            reason=data.get("reason"),
            resolved_at=data.get("resolved_at"),
            usage_unknown=bool(data.get("usage_unknown")),
            last_model=(
                str(data["last_model"]) if data.get("last_model") is not None else None
            ),
            last_provider=(
                str(data["last_provider"])
                if data.get("last_provider") is not None
                else None
            ),
            last_check_incremented_steps=(
                bool(data["last_check_incremented_steps"])
                if data.get("last_check_incremented_steps") is not None
                else None
            ),
            updated_at=float(data.get("updated_at") or time.time()),
        )

    def remaining(self, ceilings: BudgetCeilings, *, now: float | None = None) -> RemainingBudget:
        now = time.time() if now is None else now
        elapsed = max(0.0, now - self.started_at)
        return RemainingBudget(
            duration_seconds=(
                None
                if ceilings.max_duration is None
                else max(0.0, ceilings.max_duration - elapsed)
            ),
            steps=(
                None
                if ceilings.max_steps is None
                else max(0, ceilings.max_steps - self.steps)
            ),
            tokens=(
                None
                if ceilings.max_tokens is None
                else max(0, ceilings.max_tokens - self.tokens)
            ),
            usd=(
                None
                if ceilings.max_usd is None
                else max(0.0, ceilings.max_usd - self.usd)
            ),
        )


class BudgetGuardStorage:
    """Storage protocol for per-run budget state."""

    def get(self, scope_key: str) -> BudgetRunState | None:
        raise NotImplementedError

    def set(self, state: BudgetRunState) -> None:
        raise NotImplementedError

    def update(
        self,
        scope_key: str,
        fn: Callable[[BudgetRunState], T],
    ) -> T:
        """Atomically load-or-create state, apply ``fn``, persist."""
        raise NotImplementedError

    def list_all(self) -> list[BudgetRunState]:
        raise NotImplementedError


class InMemoryBudgetGuardStorage(BudgetGuardStorage):
    def __init__(self) -> None:
        self._entries: dict[str, BudgetRunState] = {}
        self._lock = threading.RLock()

    def get(self, scope_key: str) -> BudgetRunState | None:
        with self._lock:
            state = self._entries.get(scope_key)
            if state is None:
                return None
            return BudgetRunState.from_dict(state.to_dict())

    def set(self, state: BudgetRunState) -> None:
        with self._lock:
            state.updated_at = time.time()
            self._entries[state.scope_key] = BudgetRunState.from_dict(state.to_dict())

    def update(
        self,
        scope_key: str,
        fn: Callable[[BudgetRunState], T],
    ) -> T:
        with self._lock:
            existing = self._entries.get(scope_key)
            state = (
                BudgetRunState.from_dict(existing.to_dict())
                if existing is not None
                else BudgetRunState(scope_key=scope_key)
            )
            result = fn(state)
            state.updated_at = time.time()
            self._entries[scope_key] = BudgetRunState.from_dict(state.to_dict())
            return result

    def list_all(self) -> list[BudgetRunState]:
        with self._lock:
            return [BudgetRunState.from_dict(s.to_dict()) for s in self._entries.values()]


class FileBudgetGuardStorage(BudgetGuardStorage):
    def __init__(self, path: str | Path) -> None:
        self._file = LockedJsonDictFile(path)
        self._lock = threading.Lock()

    def get(self, scope_key: str) -> BudgetRunState | None:
        def read(data: dict[str, dict[str, Any]]) -> BudgetRunState | None:
            raw = data.get(scope_key)
            if raw is None:
                return None
            return BudgetRunState.from_dict(raw)

        with self._lock:
            return self._file.read_modify_write_no_save(read)

    def set(self, state: BudgetRunState) -> None:
        def mutate(data: dict[str, dict[str, Any]]) -> None:
            state.updated_at = time.time()
            data[state.scope_key] = state.to_dict()

        with self._lock:
            self._file.read_modify_write(mutate)

    def update(
        self,
        scope_key: str,
        fn: Callable[[BudgetRunState], T],
    ) -> T:
        def mutate(data: dict[str, dict[str, Any]]) -> T:
            raw = data.get(scope_key)
            state = (
                BudgetRunState.from_dict(raw)
                if raw is not None
                else BudgetRunState(scope_key=scope_key)
            )
            result = fn(state)
            state.updated_at = time.time()
            data[scope_key] = state.to_dict()
            return result

        with self._lock:
            return self._file.read_modify_write(mutate)

    def list_all(self) -> list[BudgetRunState]:
        def read(data: dict[str, dict[str, Any]]) -> list[BudgetRunState]:
            return [BudgetRunState.from_dict(raw) for raw in data.values()]

        with self._lock:
            return self._file.read_modify_write_no_save(read)


class SqliteBudgetGuardStorage(BudgetGuardStorage):
    """SQLite JSON-blob store with transactional ``update()``."""

    def __init__(self, path: str | Path, *, table: str = "mycelium_budget") -> None:
        import re as _re
        import sqlite3

        if not _re.fullmatch(r"^[a-z][a-z0-9_]*$", table):
            raise ValueError(f"invalid SQLite table name {table!r}")
        self._path = Path(path)
        self._table = table
        self._lock = threading.Lock()
        self._sqlite3 = sqlite3
        self._schema_ready = False

    def _connect(self) -> Any:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._sqlite3.connect(
            str(self._path),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = self._sqlite3.Row
        return conn

    def _ensure_schema(self, conn: Any) -> None:
        if self._schema_ready:
            return
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "scope_key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        conn.commit()
        self._schema_ready = True

    def get(self, scope_key: str) -> BudgetRunState | None:
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    f"SELECT payload FROM {self._table} WHERE scope_key = ?",  # nosec B608
                    (scope_key,),
                ).fetchone()
        if row is None:
            return None
        return BudgetRunState.from_dict(json.loads(row["payload"]))

    def set(self, state: BudgetRunState) -> None:
        state.updated_at = time.time()
        payload = json.dumps(state.to_dict(), default=str)
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                conn.execute(
                    f"INSERT INTO {self._table} (scope_key, payload) VALUES (?, ?) "  # nosec B608
                    "ON CONFLICT(scope_key) DO UPDATE SET payload = excluded.payload",
                    (state.scope_key, payload),
                )
                conn.commit()

    def update(
        self,
        scope_key: str,
        fn: Callable[[BudgetRunState], T],
    ) -> T:
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"SELECT payload FROM {self._table} WHERE scope_key = ?",  # nosec B608
                    (scope_key,),
                ).fetchone()
                state = (
                    BudgetRunState.from_dict(json.loads(row["payload"]))
                    if row is not None
                    else BudgetRunState(scope_key=scope_key)
                )
                result = fn(state)
                state.updated_at = time.time()
                conn.execute(
                    f"INSERT INTO {self._table} (scope_key, payload) VALUES (?, ?) "  # nosec B608
                    "ON CONFLICT(scope_key) DO UPDATE SET payload = excluded.payload",
                    (scope_key, json.dumps(state.to_dict(), default=str)),
                )
                conn.commit()
                return result

    def list_all(self) -> list[BudgetRunState]:
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                rows = conn.execute(f"SELECT payload FROM {self._table}").fetchall()  # nosec B608
        return [BudgetRunState.from_dict(json.loads(r["payload"])) for r in rows]


class RedisBudgetGuardStorage(BudgetGuardStorage):
    """Redis JSON-blob store with WATCH/MULTI atomic ``update()``."""

    def __init__(self, url: str, *, prefix: str = "mycelium:budget:") -> None:
        import redis
        from redis.exceptions import WatchError

        self._redis = redis.Redis.from_url(url, decode_responses=True)
        self._watch_error = WatchError
        self._prefix = prefix
        self._lock = threading.Lock()

    def _key(self, scope_key: str) -> str:
        return f"{self._prefix}{scope_key}"

    def get(self, scope_key: str) -> BudgetRunState | None:
        raw = self._redis.get(self._key(scope_key))
        if raw is None:
            return None
        return BudgetRunState.from_dict(json.loads(raw))

    def set(self, state: BudgetRunState) -> None:
        state.updated_at = time.time()
        self._redis.set(self._key(state.scope_key), json.dumps(state.to_dict(), default=str))

    def update(
        self,
        scope_key: str,
        fn: Callable[[BudgetRunState], T],
    ) -> T:
        key = self._key(scope_key)
        with self._lock:
            while True:
                with self._redis.pipeline() as pipe:
                    try:
                        pipe.watch(key)
                        raw = pipe.get(key)
                        state = (
                            BudgetRunState.from_dict(json.loads(raw))
                            if raw is not None
                            else BudgetRunState(scope_key=scope_key)
                        )
                        result = fn(state)
                        state.updated_at = time.time()
                        pipe.multi()
                        pipe.set(key, json.dumps(state.to_dict(), default=str))
                        pipe.execute()
                        return result
                    except self._watch_error:
                        continue

    def list_all(self) -> list[BudgetRunState]:
        keys = list(self._redis.scan_iter(match=f"{self._prefix}*"))
        out: list[BudgetRunState] = []
        for key in keys:
            raw = self._redis.get(key)
            if raw is None:
                continue
            out.append(BudgetRunState.from_dict(json.loads(raw)))
        return out


class PostgresBudgetGuardStorage(BudgetGuardStorage):
    """Postgres JSONB store with ``SELECT … FOR UPDATE`` atomic ``update()``."""

    def __init__(
        self,
        dsn: str,
        *,
        table: str = "mycelium_budget",
    ) -> None:
        import re as _re

        import psycopg

        if not _re.fullmatch(r"^[a-z][a-z0-9_]*$", table):
            raise ValueError(f"invalid Postgres table name {table!r}")
        self._dsn = dsn
        self._table = table
        self._psycopg = psycopg
        self._lock = threading.Lock()
        self._schema_ready = False

    def _ensure_schema(self, conn: Any) -> None:
        if self._schema_ready:
            return
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "scope_key TEXT PRIMARY KEY, payload JSONB NOT NULL)"
        )
        conn.commit()
        self._schema_ready = True

    def get(self, scope_key: str) -> BudgetRunState | None:
        with self._lock:
            with self._psycopg.connect(self._dsn) as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    f"SELECT payload FROM {self._table} WHERE scope_key = %s",  # nosec B608
                    (scope_key,),
                ).fetchone()
        if row is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return BudgetRunState.from_dict(dict(payload))

    def set(self, state: BudgetRunState) -> None:
        state.updated_at = time.time()
        payload = json.dumps(state.to_dict(), default=str)
        with self._lock:
            with self._psycopg.connect(self._dsn) as conn:
                self._ensure_schema(conn)
                conn.execute(
                    f"INSERT INTO {self._table} (scope_key, payload) VALUES (%s, %s::jsonb) "  # nosec B608
                    "ON CONFLICT (scope_key) DO UPDATE SET payload = EXCLUDED.payload",
                    (state.scope_key, payload),
                )
                conn.commit()

    def update(
        self,
        scope_key: str,
        fn: Callable[[BudgetRunState], T],
    ) -> T:
        with self._lock:
            with self._psycopg.connect(self._dsn) as conn:
                self._ensure_schema(conn)
                with conn.transaction():
                    row = conn.execute(
                        f"SELECT payload FROM {self._table} "  # nosec B608
                        "WHERE scope_key = %s FOR UPDATE",
                        (scope_key,),
                    ).fetchone()
                    if row is None:
                        state = BudgetRunState(scope_key=scope_key)
                    else:
                        payload = row[0]
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        state = BudgetRunState.from_dict(dict(payload))
                    result = fn(state)
                    state.updated_at = time.time()
                    conn.execute(
                        f"INSERT INTO {self._table} (scope_key, payload) "  # nosec B608
                        "VALUES (%s, %s::jsonb) "
                        "ON CONFLICT (scope_key) DO UPDATE SET payload = EXCLUDED.payload",
                        (scope_key, json.dumps(state.to_dict(), default=str)),
                    )
                    return result

    def list_all(self) -> list[BudgetRunState]:
        with self._lock:
            with self._psycopg.connect(self._dsn) as conn:
                self._ensure_schema(conn)
                rows = conn.execute(f"SELECT payload FROM {self._table}").fetchall()  # nosec B608
        out: list[BudgetRunState] = []
        for row in rows:
            payload = row[0]
            if isinstance(payload, str):
                payload = json.loads(payload)
            out.append(BudgetRunState.from_dict(dict(payload)))
        return out


def _ratio(used: float, maximum: float) -> float:
    if maximum <= 0:
        return 1.0
    return used / maximum


class BudgetGuard:
    """Run-scoped budget ceilings (duration / steps / tokens / USD)."""

    def __init__(
        self,
        storage: BudgetGuardStorage | None = None,
        *,
        ceilings: BudgetCeilings | None = None,
        max_duration: float | str | None = None,
        max_steps: int | None = None,
        max_tokens: int | None = None,
        max_usd: float | None = None,
        warn_at: float = 0.8,
        on_missing_meter: str = ON_MISSING_HARD,
        missing_usage_policy: str = MISSING_USAGE_POLICY_WARN,
        exclude: list[str] | None = None,
    ) -> None:
        if on_missing_meter not in ON_MISSING_METER_MODES:
            raise ValueError(
                f"on_missing_meter must be one of {sorted(ON_MISSING_METER_MODES)}, "
                f"got {on_missing_meter!r}"
            )
        if missing_usage_policy not in MISSING_USAGE_POLICIES:
            raise ValueError(
                f"missing_usage_policy must be one of "
                f"{sorted(MISSING_USAGE_POLICIES)}, got {missing_usage_policy!r}"
            )
        if not 0.0 < warn_at <= 1.0:
            raise ValueError("warn_at must be in (0, 1]")
        if ceilings is None:
            duration = (
                parse_duration_seconds(max_duration)
                if max_duration is not None
                else None
            )
            ceilings = BudgetCeilings(
                max_duration=duration,
                max_steps=max_steps,
                max_tokens=max_tokens,
                max_usd=max_usd,
            )
        self._storage = storage or InMemoryBudgetGuardStorage()
        self._ceilings = ceilings
        self._warn_at = float(warn_at)
        self._on_missing_meter = on_missing_meter
        self._missing_usage_policy = missing_usage_policy
        self._exclude = frozenset(exclude or [])

    @property
    def missing_usage_policy(self) -> str:
        return self._missing_usage_policy

    @property
    def storage(self) -> BudgetGuardStorage:
        return self._storage

    @property
    def ceilings(self) -> BudgetCeilings:
        return self._ceilings

    def get_state(
        self,
        scope_key: str | None = None,
        *,
        kwargs: dict[str, Any] | None = None,
    ) -> BudgetRunState | None:
        """Return state for an explicit or currently active execution scope."""
        key = scope_key or resolve_loop_scope_key(kwargs=kwargs)
        if key is None:
            return None
        return self._storage.get(key)

    def remaining_budget(
        self,
        scope_key: str | None = None,
        *,
        kwargs: dict[str, Any] | None = None,
    ) -> RemainingBudget | None:
        """Return runway for an explicit or currently active execution scope.

        A hard-blocked run still has a deterministic snapshot (normally zero
        in the blocked dimension). ``None`` means no scope key could be
        resolved, not that the run is blocked. Pass ``scope_key`` when querying
        after leaving ``execution_scope``.
        """
        key = scope_key or resolve_loop_scope_key(kwargs=kwargs)
        if key is None:
            return None
        state = self._storage.get(key)
        if state is None:
            state = BudgetRunState(scope_key=key)
        return state.remaining(self._ceilings)

    def check(
        self,
        kind: str = KIND_TOOL,
        *,
        scope_key: str | None = None,
        kwargs: dict[str, Any] | None = None,
        tool: str | None = None,
        increment_steps: bool = True,
    ) -> BudgetRunState:
        """Gate the next consequential step; optionally reserve one step.

        Call before an LLM turn (``kind="llm"``) or tool body (``kind="tool"``).
        Hard-blocks raise ``LedgerHardBlockError``. Soft warns
        (``warn_at``) emit ``warnings.warn`` once per dimension and **allow**
        the step — they must not shrink the declared ceiling.
        """
        global _SCOPE_MISSING_WARNED

        if kind not in STEP_KINDS:
            raise ValueError(f"kind must be one of {sorted(STEP_KINDS)}, got {kind!r}")
        if tool is not None and tool in self._exclude:
            key = scope_key or resolve_loop_scope_key(kwargs=kwargs)
            if key is None:
                return BudgetRunState(scope_key="excluded")
            return self._storage.get(key) or BudgetRunState(scope_key=key)

        if kind == KIND_LLM:
            key = scope_key or resolve_run_id(kwargs=kwargs)
        else:
            key = scope_key or resolve_loop_scope_key(kwargs=kwargs)
        if key is None:
            if not _SCOPE_MISSING_WARNED:
                warnings.warn(
                    "BudgetGuard skipped: no run_id or thread_id in execution scope; "
                    "wire transition.scope_from / execution_scope for budget protection.",
                    stacklevel=2,
                )
                _SCOPE_MISSING_WARNED = True
            return BudgetRunState(scope_key="unscoped")

        outcome: dict[str, Any] = {"exc": None, "state": None, "soft": None}

        def apply(state: BudgetRunState) -> BudgetRunState:
            now = time.time()
            if state.operator_resolution == VERIFIED_ABORT_RUN:
                state.hard_blocked = True

            if (
                kind == KIND_LLM
                and state.usage_unknown
                and self._missing_usage_policy == MISSING_USAGE_POLICY_ERROR
            ):
                outcome["exc"] = BudgetAccountingError(
                    f"BudgetGuard: run {key!r} has unknown LLM usage "
                    f"(missing_usage_policy={MISSING_USAGE_POLICY_ERROR!r}); "
                    "later LLM calls are blocked. Wire a usage extractor or "
                    "record_usage() — token counts are never invented.",
                    scope_key=key,
                )
                outcome["state"] = state
                return state

            if state.hard_blocked and not state.allow_once:
                outcome["exc"] = LedgerHardBlockError(
                    f"BudgetGuard: run {key!r} is hard-blocked"
                    + (
                        f" ({state.blocked_dimension})"
                        if state.blocked_dimension
                        else ""
                    )
                    + f". Release with: mycelium budget release {key} "
                    f"--verified clear|allow-once|abort-run --by … --reason …"
                )
                outcome["state"] = state
                return state

            missing = self._missing_meter_verdict(state)
            if missing is not None:
                outcome["exc"] = missing
                outcome["state"] = state
                return state

            pending = 1 if increment_steps else 0
            hard = self._hard_dimension(state, now=now, pending_steps=pending)
            if hard is not None:
                if state.allow_once:
                    state.allow_once = False
                else:
                    state.hard_blocked = True
                    state.blocked_dimension = hard
                    state.operator_resolution = None
                    state.resolved_by = None
                    state.reason = None
                    state.resolved_at = None
                    outcome["exc"] = LedgerHardBlockError(
                        f"BudgetGuard: run {key!r} exceeded {hard}. "
                        f"Release with: mycelium budget release {key} "
                        f"--verified clear|allow-once|abort-run --by … --reason …"
                    )
                    outcome["state"] = state
                    return state

            soft = self._soft_dimension(state, now=now, pending_steps=pending)
            if soft is not None and not state.soft_issued.get(soft):
                state.soft_issued[soft] = True
                remaining = state.remaining(self._ceilings, now=now)
                outcome["soft"] = (
                    f"BudgetGuard: approaching {soft} ceiling "
                    f"(warn_at={self._warn_at}). Remaining: {remaining.to_dict()}. "
                    "Step allowed; hard-block only at the declared ceiling."
                )

            if increment_steps:
                state.steps += 1
            state.last_check_incremented_steps = increment_steps
            outcome["state"] = state
            return state

        self._storage.update(key, apply)
        exc = outcome["exc"]
        if exc is not None:
            raise exc
        soft_msg = outcome["soft"]
        if soft_msg is not None:
            warnings.warn(soft_msg, stacklevel=2)
        assert outcome["state"] is not None
        return outcome["state"]

    def record_usage(
        self,
        *,
        scope_key: str | None = None,
        kwargs: dict[str, Any] | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        tokens: int | None = None,
        usd: float | None = None,
        steps: int = 0,
        model: str | None = None,
        provider: str | None = None,
    ) -> BudgetRunState:
        """Atomically add host-reported usage and gate for the next step.

        Prefer ``check()`` to reserve steps before work; use ``record_usage``
        after the host observes token/USD spend. ``check()`` and the budget
        decorators auto-meter one step by default. Passing ``steps`` here is
        supported for fully manual integrations; it warns unless the current
        run's latest ``check()`` used ``increment_steps=False``, because
        combining it with automatic accounting double-counts steps.
        """
        global _SCOPE_MISSING_WARNED

        key = scope_key or resolve_run_id(kwargs=kwargs) or resolve_loop_scope_key(
            kwargs=kwargs
        )
        if key is None:
            if not _SCOPE_MISSING_WARNED:
                warnings.warn(
                    "BudgetGuard.record_usage skipped: no run_id or thread_id "
                    "in execution scope.",
                    stacklevel=2,
                )
                _SCOPE_MISSING_WARNED = True
            return BudgetRunState(scope_key="unscoped")

        add_in = int(tokens_in or 0)
        add_out = int(tokens_out or 0)
        if tokens is not None:
            add_in += int(tokens)
        add_usd = float(usd or 0.0)
        add_steps = int(steps)
        if add_in < 0 or add_out < 0 or add_usd < 0 or add_steps < 0:
            raise ValueError("usage deltas must be non-negative")

        outcome: dict[str, Any] = {"exc": None, "state": None, "warning": None}

        def apply(state: BudgetRunState) -> BudgetRunState:
            if add_steps and state.last_check_incremented_steps is not False:
                outcome["warning"] = (
                    "BudgetGuard.record_usage(steps=...) adds host-reported steps, "
                    "but check() and @budget_guard auto-meter one step by default. "
                    "Use steps=0, or use check(increment_steps=False) in a fully "
                    "manual integration, to avoid double metering."
                )
            if state.hard_blocked and not state.allow_once:
                outcome["exc"] = LedgerHardBlockError(
                    f"BudgetGuard: run {key!r} is hard-blocked"
                    + (
                        f" ({state.blocked_dimension})"
                        if state.blocked_dimension
                        else ""
                    )
                    + f". Release with: mycelium budget release {key} "
                    f"--verified clear|allow-once|abort-run --by … --reason …"
                )
                outcome["state"] = state
                return state

            state.tokens_in += add_in
            state.tokens_out += add_out
            state.usd += add_usd
            state.steps += add_steps
            if model:
                state.last_model = str(model)
            if provider:
                state.last_provider = str(provider)

            now = time.time()
            hard = self._hard_dimension(state, now=now, pending_steps=0)
            if hard is not None:
                if state.allow_once:
                    state.allow_once = False
                else:
                    state.hard_blocked = True
                    state.blocked_dimension = hard
                    state.operator_resolution = None
                    state.resolved_by = None
                    state.reason = None
                    state.resolved_at = None
            outcome["state"] = state
            return state

        self._storage.update(key, apply)
        warning = outcome["warning"]
        if warning is not None:
            warnings.warn(warning, UserWarning, stacklevel=2)
        exc = outcome["exc"]
        if exc is not None:
            raise exc
        assert outcome["state"] is not None
        return outcome["state"]

    def release(
        self,
        scope_key: str,
        *,
        verified: str,
        by: str,
        reason: str,
    ) -> BudgetRunState:
        """Operator release for a hard-blocked (or abortable) run."""
        if verified not in VERIFIED_RESOLUTIONS:
            raise LedgerReleaseRefusedError(
                f"unknown --verified {verified!r}; "
                f"expected one of {sorted(VERIFIED_RESOLUTIONS)}"
            )

        outcome: dict[str, Any] = {"exc": None, "state": None}

        def apply(state: BudgetRunState) -> BudgetRunState:
            if state.operator_resolution is not None:
                outcome["exc"] = LedgerAlreadyResolvedError(
                    f"run {scope_key!r} already released "
                    f"({state.operator_resolution!r} by {state.resolved_by!r})"
                )
                outcome["state"] = state
                return state
            if not state.hard_blocked and verified != VERIFIED_ABORT_RUN:
                outcome["exc"] = LedgerReleaseRefusedError(
                    f"run {scope_key!r} is not hard-blocked; nothing to release"
                )
                outcome["state"] = state
                return state

            now = time.time()
            state.operator_resolution = verified
            state.resolved_by = by
            state.reason = reason
            state.resolved_at = now

            if verified == VERIFIED_CLEAR:
                state.hard_blocked = False
                state.allow_once = False
                state.blocked_dimension = None
                state.soft_issued = {}
                state.steps = 0
                state.tokens_in = 0
                state.tokens_out = 0
                state.usd = 0.0
                state.last_check_incremented_steps = None
                state.started_at = now
            elif verified == VERIFIED_ALLOW_ONCE:
                state.hard_blocked = False
                state.allow_once = True
                state.blocked_dimension = None
            else:  # abort-run
                state.hard_blocked = True
                state.allow_once = False

            outcome["state"] = state
            return state

        # load-or-create so missing keys refuse cleanly
        existing = self._storage.get(scope_key)
        if existing is None:
            raise LedgerReleaseRefusedError(
                f"no budget-guard state for run {scope_key!r}"
            )

        self._storage.update(scope_key, apply)
        if outcome["exc"] is not None:
            raise outcome["exc"]
        assert outcome["state"] is not None
        return outcome["state"]

    def note_missing_usage(
        self,
        *,
        scope_key: str | None = None,
        kwargs: dict[str, Any] | None = None,
        reason: str = "no token usage in the provider/framework response",
    ) -> None:
        """Handle a completed or closed LLM turn with no measurable usage.

        ``warn`` (default): one bounded warning per run; do not record zero.
        ``error``: mark the run's LLM accounting unknown and raise
        ``BudgetAccountingError`` so later LLM calls are blocked.
        """
        key = scope_key or resolve_run_id(kwargs=kwargs)
        if key is None:
            return
        msg = (
            f"BudgetGuard: run {key!r} LLM turn had no measurable usage "
            f"({reason}). Token counts are never invented."
        )
        if self._missing_usage_policy == MISSING_USAGE_POLICY_WARN:
            if key not in _MISSING_USAGE_WARNED:
                warnings.warn(msg, UserWarning, stacklevel=3)
                _MISSING_USAGE_WARNED.add(key)
            return

        def apply(state: BudgetRunState) -> BudgetRunState:
            state.usage_unknown = True
            state.blocked_dimension = VIOLATION_MISSING_USAGE
            return state

        self._storage.update(key, apply)
        raise BudgetAccountingError(
            msg
            + f" missing_usage_policy={MISSING_USAGE_POLICY_ERROR!r}; "
            "later LLM calls for this run are blocked. Register a usage "
            "extractor or call record_usage() with real counts.",
            scope_key=key,
        )

    def _missing_meter_verdict(self, state: BudgetRunState) -> Exception | None:
        if not self._ceilings.requires_usage_meter():
            return None
        if self._on_missing_meter == ON_MISSING_OFF:
            return None
        if state.steps < 1:
            return None
        if state.has_recorded_usage:
            return None
        msg = (
            f"BudgetGuard: run {state.scope_key!r} declared max_tokens/max_usd "
            "but record_usage was never called after steps ran "
            f"(on_missing_meter={self._on_missing_meter!r})."
        )
        if self._on_missing_meter == ON_MISSING_WARN:
            if state.scope_key not in _MISSING_METER_WARNED:
                warnings.warn(msg, stacklevel=3)
                _MISSING_METER_WARNED.add(state.scope_key)
            return None
        state.hard_blocked = True
        state.blocked_dimension = VIOLATION_MISSING_METER
        return LedgerHardBlockError(
            msg
            + f" Release with: mycelium budget release {state.scope_key} "
            "--verified clear|allow-once|abort-run --by … --reason …"
        )

    def _hard_dimension(
        self,
        state: BudgetRunState,
        *,
        now: float,
        pending_steps: int,
    ) -> str | None:
        """Return the budget ceiling that blocks the next operation.

        With automatic accounting (``pending_steps=1``), ``max_steps=N``
        allows exactly N reservations and blocks when the next reservation
        would exceed N. With manual accounting (``pending_steps=0``), the
        host records completed steps separately, so checks block once the
        recorded count has reached N.
        """
        c = self._ceilings
        if c.max_duration is not None and (now - state.started_at) >= c.max_duration:
            return "max_duration"
        if c.max_steps is not None and (
            state.steps + pending_steps > c.max_steps
            or (pending_steps == 0 and state.steps >= c.max_steps)
        ):
            return "max_steps"
        if c.max_tokens is not None and state.tokens >= c.max_tokens:
            return "max_tokens"
        if c.max_usd is not None and state.usd >= c.max_usd:
            return "max_usd"
        return None

    def _soft_dimension(
        self,
        state: BudgetRunState,
        *,
        now: float,
        pending_steps: int,
    ) -> str | None:
        """Warn when meters reach ``warn_at`` of a ceiling (permissive).

        Uses committed meters only (not the pending step), so a warn never
        steals the final allowed unit. ``pending_steps`` is accepted for API
        symmetry with ``_hard_dimension`` and ignored.
        """
        _ = pending_steps  # warn on committed usage; do not project the next step
        c = self._ceilings
        if c.max_duration is not None:
            elapsed = now - state.started_at
            if (
                elapsed < c.max_duration
                and _ratio(elapsed, c.max_duration) >= self._warn_at
            ):
                return "max_duration"
        if c.max_steps is not None and c.max_steps > 0:
            if (
                state.steps < c.max_steps
                and _ratio(float(state.steps), float(c.max_steps)) >= self._warn_at
            ):
                return "max_steps"
        if c.max_tokens is not None and c.max_tokens > 0:
            if (
                state.tokens < c.max_tokens
                and _ratio(float(state.tokens), float(c.max_tokens)) >= self._warn_at
            ):
                return "max_tokens"
        if c.max_usd is not None and c.max_usd > 0:
            if (
                state.usd < c.max_usd
                and _ratio(state.usd, c.max_usd) >= self._warn_at
            ):
                return "max_usd"
        return None


def _mark_budget_guarded(func: Callable[..., Any]) -> None:
    func._mycelium_budget_guarded = True  # type: ignore[attr-defined]


def budget_guard(
    guard: BudgetGuard,
    *,
    tool_name: str | None = None,
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Async decorator: ``BudgetGuard.check(kind='tool')`` before the body."""

    def decorator(func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        name = tool_name or func.__name__

        @functools.wraps(func)
        async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            guard.check(KIND_TOOL, kwargs=dict(kwargs), tool=name)
            return await func(*args, **kwargs)

        _mark_budget_guarded(wrapper)
        return wrapper

    return decorator


def budget_guard_sync(
    guard: BudgetGuard,
    *,
    tool_name: str | None = None,
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Sync decorator: ``BudgetGuard.check(kind='tool')`` before the body."""

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        name = tool_name or func.__name__

        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            guard.check(KIND_TOOL, kwargs=dict(kwargs), tool=name)
            return func(*args, **kwargs)

        _mark_budget_guarded(wrapper)
        return wrapper

    return decorator


def apply_budget_guard(
    func: Callable[..., Any],
    guard: BudgetGuard,
    *,
    tool_name: str | None = None,
) -> Callable[..., Any]:
    """Apply sync or async budget_guard wrapper based on ``func``."""
    name = tool_name or getattr(func, "__name__", "tool")
    if inspect.iscoroutinefunction(func):
        return budget_guard(guard, tool_name=name)(func)
    return budget_guard_sync(guard, tool_name=name)(func)


__all__ = [
    "KIND_LLM",
    "KIND_TOOL",
    "MISSING_USAGE_POLICIES",
    "MISSING_USAGE_POLICY_ERROR",
    "MISSING_USAGE_POLICY_WARN",
    "ON_MISSING_HARD",
    "ON_MISSING_METER_MODES",
    "ON_MISSING_OFF",
    "ON_MISSING_WARN",
    "STEP_KINDS",
    "VIOLATION_BUDGET",
    "VIOLATION_BUDGET_WARN",
    "VIOLATION_MISSING_METER",
    "VIOLATION_MISSING_USAGE",
    "BudgetAccountingError",
    "BudgetCeilings",
    "BudgetGuard",
    "BudgetGuardStorage",
    "BudgetRunState",
    "FileBudgetGuardStorage",
    "InMemoryBudgetGuardStorage",
    "PostgresBudgetGuardStorage",
    "RedisBudgetGuardStorage",
    "RemainingBudget",
    "SqliteBudgetGuardStorage",
    "apply_budget_guard",
    "budget_guard",
    "budget_guard_sync",
    "parse_duration_seconds",
]
