"""Host-issued object grants for destructive / irreversible tool calls.

Tool permission is not object authorization. A configured destructive tool
may claim, execute, or cross a side-effect boundary only when the host has
minted an exact grant: this operation, this canonical object, this scope
when bound, before expiry, for at most ``max_uses``. The model cannot
create, widen, renew, or approve a grant. Dual control is intentionally
not implemented — two-person approval belongs in the host workflow that
calls :func:`issue_destructive_grant`.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import json
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, ParamSpec, Protocol, TypeVar

from mycelium.entity_guard import PAYLOAD_OMITTED
from mycelium.storage.json_file import LockedJsonDictFile
from mycelium.tool_boundary import ToolBoundaryError
from mycelium.transition import LEDGER_KWARG_KEYS, get_active_execution_scope

P = ParamSpec("P")
R = TypeVar("R")

MISSING_POLICY_ERROR = "error"
MISSING_POLICY_WARN = "warn"
MISSING_POLICIES = frozenset({MISSING_POLICY_ERROR, MISSING_POLICY_WARN})

DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"
DECISION_EXPIRED = "expired"
DECISION_EXHAUSTED = "exhausted"
DECISION_MISMATCHED = "mismatched"
DECISION_AMBIGUOUS = "ambiguous"
DECISIONS = frozenset(
    {
        DECISION_ALLOWED,
        DECISION_DENIED,
        DECISION_EXPIRED,
        DECISION_EXHAUSTED,
        DECISION_MISMATCHED,
        DECISION_AMBIGUOUS,
    }
)

REASON_MISSING = "missing"
REASON_EXPIRED = "expired"
REASON_EXHAUSTED = "exhausted"
REASON_MISMATCHED = "mismatched"
REASON_MALFORMED = "malformed"
REASON_UNVERIFIABLE = "unverifiable"
REASON_STORAGE = "storage"
REASON_RETRY = "retry"
REASON_IDENTITY_DRIFT = "identity_drift"

PROVENANCE_HOST = "host"
STORAGE_MEMORY = "memory"
STORAGE_FILE = "file"
STORAGE_SQLITE = "sqlite"
STORAGE_REDIS = "redis"
STORAGE_POSTGRES = "postgres"
DURABLE_STORAGES = frozenset({STORAGE_FILE, STORAGE_SQLITE, STORAGE_REDIS, STORAGE_POSTGRES})
SINGLE_NODE_GRANT_STORAGES = frozenset({STORAGE_FILE, STORAGE_SQLITE, STORAGE_MEMORY})
SHARED_GRANT_STORAGES = frozenset({STORAGE_REDIS, STORAGE_POSTGRES})

_HOMOGLYPH_DOTS = ("\u3002", "\uff0e", "\uff61", "\u2024")
_DYNAMIC_MARKERS = ("{", "}", "{{", "}}", "${")
_SCHEME_PREFIXES = ("http:", "https:", "file:", "ftp:", "data:")
_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_MISSING = object()
_PAYLOAD_FIELD_NAMES = frozenset(
    {
        "body",
        "content",
        "text",
        "html",
        "message",
        "payload",
        "file",
        "data",
        "attachments",
        "subject",
        "title",
        "description",
        "markdown",
        "bytes",
        "amount",
        "reason",
        "notes",
        "secret",
        "token",
        "password",
        "api_key",
    }
)

_clock_var: ContextVar[Callable[[], float] | None] = ContextVar(
    "mycelium_destructive_clock", default=None
)
_store_var: ContextVar[DestructiveGrantStore | None] = ContextVar(
    "mycelium_destructive_grant_store", default=None
)
_policy_var: ContextVar[DestructiveConfirmPolicy | None] = ContextVar(
    "mycelium_destructive_policy", default=None
)
_decision_var: ContextVar[DestructiveDecision | None] = ContextVar(
    "mycelium_destructive_decision", default=None
)
_canonicalizers: dict[str, Callable[[Any], str]] = {}
_default_store: DestructiveGrantStore | None = None


class DestructiveGrantError(ToolBoundaryError):
    """Grant missing, expired, exhausted, mismatched, or unverifiable."""

    def __init__(
        self,
        message: str,
        *,
        tool: str,
        operation: str | None = None,
        object_type: str | None = None,
        object_ref: str | None = None,
        reason: str = REASON_MISSING,
    ) -> None:
        super().__init__(
            message,
            violation="destructive_confirm",
            tool_name=tool,
            llm_message=(
                f"Destructive confirmation blocked {tool!r}: {message}. "
                "The host must issue an exact grant for this operation and "
                "object. The tool body was not executed."
            ),
            field=operation,
            expected=object_type,
            recovery_hint=(
                "Ask the host to mint a DestructiveGrant for this exact "
                "operation and canonical object. The model cannot approve "
                "or renew grants."
            ),
        )
        self.tool = tool
        self.operation = operation
        self.object_type = object_type
        self.object_ref = object_ref
        self.reason = reason


class DestructiveCanonicalizeError(ValueError):
    """Object type, object id, or operation cannot be canonicalized."""


@dataclass(frozen=True)
class DestructiveGrant:
    """Immutable host-issued authorization for one destructive attempt."""

    grant_id: str
    operation: str
    object_type: str
    object_id: str
    issued_at: float
    expires_at: float
    max_uses: int
    request_id: str | None = None
    run_id: str | None = None
    thread_id: str | None = None
    tenant: str | None = None
    account: str | None = None
    policy_version: str = "unspecified"
    policy_hash: str = ""
    provenance: str = PROVENANCE_HOST

    def to_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "operation": self.operation,
            "object_type": self.object_type,
            "object_id": self.object_id,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "max_uses": self.max_uses,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "thread_id": self.thread_id,
            "tenant": self.tenant,
            "account": self.account,
            "policy_version": self.policy_version,
            "policy_hash": self.policy_hash,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DestructiveGrant:
        required = (
            "grant_id",
            "operation",
            "object_type",
            "object_id",
            "issued_at",
            "expires_at",
            "max_uses",
        )
        missing = [key for key in required if key not in raw]
        if missing:
            raise DestructiveCanonicalizeError(
                f"grant record missing field(s): {missing}"
            )
        return cls(
            grant_id=str(raw["grant_id"]),
            operation=str(raw["operation"]),
            object_type=str(raw["object_type"]),
            object_id=str(raw["object_id"]),
            issued_at=float(raw["issued_at"]),
            expires_at=float(raw["expires_at"]),
            max_uses=int(raw["max_uses"]),
            request_id=_optional_str(raw.get("request_id")),
            run_id=_optional_str(raw.get("run_id")),
            thread_id=_optional_str(raw.get("thread_id")),
            tenant=_optional_str(raw.get("tenant")),
            account=_optional_str(raw.get("account")),
            policy_version=str(raw.get("policy_version") or "unspecified"),
            policy_hash=str(raw.get("policy_hash") or ""),
            provenance=str(raw.get("provenance") or PROVENANCE_HOST),
        )


@dataclass(frozen=True)
class DestructiveObjectSpec:
    object_type: str
    id_from: str
    tenant_from: str | None = None
    account_from: str | None = None
    case_sensitive: bool = True
    require_canonicalizer: bool = False


@dataclass(frozen=True)
class DestructiveGrantSpec:
    bind_request_id: bool = False
    bind_run_id: bool = False
    bind_thread_id: bool = False
    max_uses: int = 1
    ttl_seconds: float = 300.0


@dataclass(frozen=True)
class DestructiveToolPolicy:
    operation: str
    object: DestructiveObjectSpec
    grant: DestructiveGrantSpec = field(default_factory=DestructiveGrantSpec)


@dataclass(frozen=True)
class DestructiveConfirmPolicy:
    """Host-owned destructive-confirm policy. The model cannot mint grants."""

    enabled: bool = True
    missing_policy: str = MISSING_POLICY_ERROR
    policy_version: str = "unspecified"
    storage: str = STORAGE_MEMORY
    tools: dict[str, DestructiveToolPolicy] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.missing_policy not in MISSING_POLICIES:
            raise ValueError(
                "destructive_confirm.missing_policy must be one of "
                f"{sorted(MISSING_POLICIES)}, got {self.missing_policy!r}"
            )


@dataclass(frozen=True)
class DestructiveDecision:
    tool: str
    operation: str
    object_type: str
    object_ref: str
    tenant: str | None
    account: str | None
    grant_ref: str | None
    policy_version: str
    decision: str
    reason: str
    request_id: str | None
    run_id: str | None
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "operation": self.operation,
            "object_type": self.object_type,
            "object_ref": self.object_ref,
            "tenant": self.tenant,
            "account": self.account,
            "grant_ref": self.grant_ref,
            "policy_version": self.policy_version,
            "decision": self.decision,
            "reason": self.reason,
            "request_id": self.request_id,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class ConsumeResult:
    status: str
    grant: DestructiveGrant | None = None
    uses_remaining: int | None = None


class DestructiveGrantStore(Protocol):
    kind: str

    def put(self, grant: DestructiveGrant) -> None: ...

    def get(self, grant_id: str) -> dict[str, Any] | None: ...

    def try_consume(
        self, grant_id: str, request_id: str, now: float
    ) -> ConsumeResult: ...


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _now() -> float:
    clock = _clock_var.get()
    return float(clock()) if clock is not None else time.time()


def set_destructive_clock(clock: Callable[[], float] | None) -> Token[Callable[[], float] | None]:
    """Install an injectable clock for expiry tests. ``None`` restores wall time.

    Also binds the shared authority-window clock so authorize and use phases
    observe the same host/test time.
    """
    from mycelium.authority_window import set_authority_clock

    set_authority_clock(clock)
    return _clock_var.set(clock)


def reset_destructive_clock(token: Token[Callable[[], float] | None]) -> None:
    from mycelium.authority_window import set_authority_clock

    set_authority_clock(None)
    _clock_var.reset(token)


def get_destructive_grant_store() -> DestructiveGrantStore | None:
    return _store_var.get() or _default_store


def set_destructive_grant_store(
    store: DestructiveGrantStore | None,
) -> Token[DestructiveGrantStore | None]:
    """Bind the process-wide grant store used by :func:`issue_destructive_grant`."""
    global _default_store
    _default_store = store
    return _store_var.set(store)


def reset_destructive_grant_store(token: Token[DestructiveGrantStore | None]) -> None:
    _store_var.reset(token)


def get_active_destructive_policy() -> DestructiveConfirmPolicy | None:
    return _policy_var.get()


def set_active_destructive_policy(
    policy: DestructiveConfirmPolicy | None,
) -> Token[DestructiveConfirmPolicy | None]:
    return _policy_var.set(policy)


def reset_active_destructive_policy(token: Token[DestructiveConfirmPolicy | None]) -> None:
    _policy_var.reset(token)


def get_active_destructive_decision() -> DestructiveDecision | None:
    return _decision_var.get()


def reset_destructive_confirm_state() -> None:
    """Clear process-local grant, policy, decision, clock, and canonicalizers."""
    global _default_store
    from mycelium.authority_window import reset_authority_window_state

    _store_var.set(None)
    _policy_var.set(None)
    _decision_var.set(None)
    _clock_var.set(None)
    _default_store = None
    _canonicalizers.clear()
    reset_authority_window_state()


def register_destructive_object_canonicalizer(
    object_type: str, fn: Callable[[Any], str]
) -> None:
    """Register a host canonicalizer that runs before claim and side effect."""
    if not isinstance(object_type, str) or not object_type.strip():
        raise ValueError("object_type must be a non-empty string")
    key = canonicalize_object_type(object_type)
    _canonicalizers[key] = fn


def registered_destructive_canonicalizers() -> frozenset[str]:
    return frozenset(_canonicalizers)


def canonicalize_operation(value: Any) -> str:
    if not isinstance(value, str):
        raise DestructiveCanonicalizeError("operation must be a string")
    text = value.strip()
    if not text or text != value.strip():
        if value != value.strip() or not text:
            raise DestructiveCanonicalizeError("operation is empty or whitespace")
    if any(marker in text for marker in _DYNAMIC_MARKERS):
        raise DestructiveCanonicalizeError("operation is dynamic")
    return text.casefold()


def canonicalize_object_type(value: Any) -> str:
    if not isinstance(value, str):
        raise DestructiveCanonicalizeError("object type must be a string")
    text = value.strip()
    if not text:
        raise DestructiveCanonicalizeError("object type is empty or whitespace")
    if value != value.strip():
        raise DestructiveCanonicalizeError("object type has surrounding whitespace")
    if any(marker in text for marker in _DYNAMIC_MARKERS):
        raise DestructiveCanonicalizeError("object type is dynamic")
    return text.casefold()


def canonicalize_scope_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise DestructiveCanonicalizeError(f"{field} must be a string")
    if value != value.strip() or not value.strip():
        raise DestructiveCanonicalizeError(f"{field} is empty, whitespace, or padded")
    if any(ord(char) < 32 or char == "\x7f" for char in value):
        raise DestructiveCanonicalizeError(f"{field} contains control characters")
    return value


def canonicalize_object_id(
    value: Any,
    *,
    object_type: str,
    case_sensitive: bool = True,
) -> str:
    """Deterministic object-id canonicalization. Rejects ambiguous encodings."""
    custom = _canonicalizers.get(object_type)
    if custom is not None:
        value = custom(value)
    if not isinstance(value, str):
        raise DestructiveCanonicalizeError("object id must be a string")
    if value != value.strip() or not value.strip():
        raise DestructiveCanonicalizeError(
            "object id is empty, whitespace-only, or has surrounding whitespace"
        )
    if any(ord(char) < 32 or char == "\x7f" for char in value):
        raise DestructiveCanonicalizeError("object id contains control characters")
    if ".." in value or "\\" in value or "%" in value or "@" in value:
        raise DestructiveCanonicalizeError("object id uses a path, encoding, or userinfo form")
    if any(dot in value for dot in _HOMOGLYPH_DOTS):
        raise DestructiveCanonicalizeError("object id uses an ambiguous Unicode dot")
    folded = value.casefold()
    if any(folded.startswith(prefix) for prefix in _SCHEME_PREFIXES):
        raise DestructiveCanonicalizeError("object id looks like a URL or data URI")
    if value.startswith("/") or value.startswith("\\"):
        raise DestructiveCanonicalizeError("object id looks like an absolute path")
    if any(marker in value for marker in _DYNAMIC_MARKERS):
        raise DestructiveCanonicalizeError("object id is dynamic")
    return value if case_sensitive else folded


def object_digest(object_type: str, object_id: str) -> str:
    payload = f"{object_type}\0{object_id}".encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def grant_digest(grant_id: str) -> str:
    return hashlib.sha256(grant_id.encode()).hexdigest()[:16]


def policy_hash_for(
    *,
    operation: str,
    object_type: str,
    object_id: str,
    tenant: str | None,
    account: str | None,
    max_uses: int,
    bind_request_id: bool,
    bind_run_id: bool,
    bind_thread_id: bool,
    policy_version: str,
) -> str:
    payload = "|".join(
        [
            operation,
            object_type,
            object_id,
            tenant or "",
            account or "",
            str(max_uses),
            "1" if bind_request_id else "0",
            "1" if bind_run_id else "0",
            "1" if bind_thread_id else "0",
            policy_version,
        ]
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _record_dict(grant: DestructiveGrant) -> dict[str, Any]:
    return {
        "grant": grant.to_dict(),
        "uses_remaining": grant.max_uses,
        "consumed_request_ids": [],
    }


def _consume_record(
    rec: dict[str, Any] | None, request_id: str, now: float
) -> ConsumeResult:
    if rec is None:
        return ConsumeResult(status=REASON_MISSING)
    try:
        grant = DestructiveGrant.from_dict(rec["grant"])
        uses_remaining = int(rec.get("uses_remaining", 0))
        consumed = [str(item) for item in rec.get("consumed_request_ids") or []]
    except (KeyError, TypeError, ValueError, DestructiveCanonicalizeError):
        return ConsumeResult(status=DECISION_AMBIGUOUS)
    if request_id and request_id in consumed:
        return ConsumeResult(status=REASON_RETRY, grant=grant, uses_remaining=uses_remaining)
    if now >= float(grant.expires_at):
        return ConsumeResult(status=REASON_EXPIRED, grant=grant, uses_remaining=uses_remaining)
    if uses_remaining <= 0:
        return ConsumeResult(status=REASON_EXHAUSTED, grant=grant, uses_remaining=0)
    rec["uses_remaining"] = uses_remaining - 1
    consumed.append(request_id)
    rec["consumed_request_ids"] = consumed
    return ConsumeResult(
        status=DECISION_ALLOWED,
        grant=grant,
        uses_remaining=uses_remaining - 1,
    )


class InMemoryDestructiveGrantStore:
    """Process-local grant store. Development and tests only."""

    kind = STORAGE_MEMORY

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}

    def put(self, grant: DestructiveGrant) -> None:
        with self._lock:
            self._records[grant.grant_id] = _record_dict(grant)

    def get(self, grant_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._records.get(grant_id)
            return json.loads(json.dumps(rec)) if rec is not None else None

    def try_consume(self, grant_id: str, request_id: str, now: float) -> ConsumeResult:
        with self._lock:
            rec = self._records.get(grant_id)
            result = _consume_record(rec, request_id, now)
            return result


class FileDestructiveGrantStore:
    """Locked JSON file store for single-node deployments."""

    kind = STORAGE_FILE

    def __init__(self, path: str | Path) -> None:
        self._file = LockedJsonDictFile(path)

    def put(self, grant: DestructiveGrant) -> None:
        def mutate(data: dict[str, dict[str, Any]]) -> None:
            data[grant.grant_id] = _record_dict(grant)

        self._file.read_modify_write(mutate)

    def get(self, grant_id: str) -> dict[str, Any] | None:
        data = self._file.load()
        rec = data.get(grant_id)
        return dict(rec) if rec is not None else None

    def try_consume(self, grant_id: str, request_id: str, now: float) -> ConsumeResult:
        def mutate(data: dict[str, dict[str, Any]]) -> ConsumeResult:
            rec = data.get(grant_id)
            result = _consume_record(rec, request_id, now)
            return result

        return self._file.read_modify_write(mutate)


class SqliteDestructiveGrantStore:
    """SQLite JSON-blob store with ``BEGIN IMMEDIATE`` consume."""

    kind = STORAGE_SQLITE

    def __init__(self, path: str | Path, *, table: str = "mycelium_destructive_grants") -> None:
        import sqlite3

        if not _TABLE_RE.fullmatch(table):
            raise ValueError(f"invalid SQLite table name {table!r}")
        self._path = Path(path)
        self._table = table
        self._lock = threading.Lock()
        self._sqlite3 = sqlite3
        self._schema_ready = False

    def _connect(self) -> Any:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._sqlite3.connect(
            str(self._path), timeout=30.0, check_same_thread=False
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
            "grant_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        conn.commit()
        self._schema_ready = True

    def put(self, grant: DestructiveGrant) -> None:
        payload = json.dumps(_record_dict(grant), default=str)
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                conn.execute(
                    f"INSERT INTO {self._table} (grant_id, payload) VALUES (?, ?) "  # nosec B608
                    "ON CONFLICT(grant_id) DO UPDATE SET payload = excluded.payload",
                    (grant.grant_id, payload),
                )
                conn.commit()

    def get(self, grant_id: str) -> dict[str, Any] | None:
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    f"SELECT payload FROM {self._table} WHERE grant_id = ?",  # nosec B608
                    (grant_id,),
                ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def try_consume(self, grant_id: str, request_id: str, now: float) -> ConsumeResult:
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    f"SELECT payload FROM {self._table} WHERE grant_id = ?",  # nosec B608
                    (grant_id,),
                ).fetchone()
                rec = json.loads(row["payload"]) if row is not None else None
                result = _consume_record(rec, request_id, now)
                if rec is not None and result.status == DECISION_ALLOWED:
                    conn.execute(
                        f"UPDATE {self._table} SET payload = ? WHERE grant_id = ?",  # nosec B608
                        (json.dumps(rec, default=str), grant_id),
                    )
                conn.commit()
                return result


class RedisDestructiveGrantStore:
    """Redis JSON-blob store with WATCH/MULTI atomic consume."""

    kind = STORAGE_REDIS

    def __init__(self, url: str, *, prefix: str = "mycelium:destructive:") -> None:
        import redis
        from redis.exceptions import WatchError

        self._redis = redis.Redis.from_url(url, decode_responses=True)
        self._watch_error = WatchError
        self._prefix = prefix
        self._lock = threading.Lock()

    def _key(self, grant_id: str) -> str:
        return f"{self._prefix}{grant_id}"

    def put(self, grant: DestructiveGrant) -> None:
        self._redis.set(self._key(grant.grant_id), json.dumps(_record_dict(grant), default=str))

    def get(self, grant_id: str) -> dict[str, Any] | None:
        raw = self._redis.get(self._key(grant_id))
        if raw is None:
            return None
        return json.loads(raw)

    def try_consume(self, grant_id: str, request_id: str, now: float) -> ConsumeResult:
        key = self._key(grant_id)
        with self._lock:
            while True:
                with self._redis.pipeline() as pipe:
                    try:
                        pipe.watch(key)
                        raw = pipe.get(key)
                        rec = json.loads(raw) if raw is not None else None
                        result = _consume_record(rec, request_id, now)
                        if rec is not None and result.status == DECISION_ALLOWED:
                            pipe.multi()
                            pipe.set(key, json.dumps(rec, default=str))
                            pipe.execute()
                        else:
                            pipe.reset()
                        return result
                    except self._watch_error:
                        continue


class PostgresDestructiveGrantStore:
    """Postgres JSONB store with ``SELECT … FOR UPDATE`` consume."""

    kind = STORAGE_POSTGRES

    def __init__(self, dsn: str, *, table: str = "mycelium_destructive_grants") -> None:
        import psycopg

        if not _TABLE_RE.fullmatch(table):
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
            "grant_id TEXT PRIMARY KEY, payload JSONB NOT NULL)"
        )
        conn.commit()
        self._schema_ready = True

    def put(self, grant: DestructiveGrant) -> None:
        payload = json.dumps(_record_dict(grant), default=str)
        with self._lock:
            with self._psycopg.connect(self._dsn) as conn:
                self._ensure_schema(conn)
                conn.execute(
                    f"INSERT INTO {self._table} (grant_id, payload) VALUES (%s, %s::jsonb) "  # nosec B608
                    "ON CONFLICT (grant_id) DO UPDATE SET payload = EXCLUDED.payload",
                    (grant.grant_id, payload),
                )
                conn.commit()

    def get(self, grant_id: str) -> dict[str, Any] | None:
        with self._lock:
            with self._psycopg.connect(self._dsn) as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    f"SELECT payload FROM {self._table} WHERE grant_id = %s",  # nosec B608
                    (grant_id,),
                ).fetchone()
        if row is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return dict(payload)

    def try_consume(self, grant_id: str, request_id: str, now: float) -> ConsumeResult:
        with self._lock:
            with self._psycopg.connect(self._dsn) as conn:
                self._ensure_schema(conn)
                with conn.transaction():
                    row = conn.execute(
                        f"SELECT payload FROM {self._table} "  # nosec B608
                        "WHERE grant_id = %s FOR UPDATE",
                        (grant_id,),
                    ).fetchone()
                    if row is None:
                        rec = None
                    else:
                        payload = row[0]
                        if isinstance(payload, str):
                            payload = json.loads(payload)
                        rec = dict(payload)
                    result = _consume_record(rec, request_id, now)
                    if rec is not None and result.status == DECISION_ALLOWED:
                        conn.execute(
                            f"UPDATE {self._table} SET payload = %s::jsonb "  # nosec B608
                            "WHERE grant_id = %s",
                            (json.dumps(rec, default=str), grant_id),
                        )
                    return result


def issue_destructive_grant(
    *,
    operation: str,
    object_type: str,
    object_id: str,
    request_id: str | None = None,
    run_id: str | None = None,
    thread_id: str | None = None,
    tenant: str | None = None,
    account: str | None = None,
    expires_in: float = 300.0,
    max_uses: int = 1,
    policy_version: str = "unspecified",
    store: DestructiveGrantStore | None = None,
    case_sensitive: bool = True,
    bind_request_id: bool = False,
    bind_run_id: bool = False,
    bind_thread_id: bool = False,
) -> DestructiveGrant:
    """Mint a host-controlled grant. Never call this from model-controlled code."""
    if not isinstance(expires_in, (int, float)) or expires_in <= 0:
        raise ValueError("expires_in must be a positive number of seconds")
    if not isinstance(max_uses, int) or isinstance(max_uses, bool) or max_uses < 1:
        raise ValueError("max_uses must be an integer >= 1")
    op = canonicalize_operation(operation)
    otype = canonicalize_object_type(object_type)
    oid = canonicalize_object_id(object_id, object_type=otype, case_sensitive=case_sensitive)
    tenant_id = canonicalize_scope_id(tenant, field="tenant") if tenant is not None else None
    account_id = canonicalize_scope_id(account, field="account") if account is not None else None
    req = canonicalize_scope_id(request_id, field="request_id") if request_id is not None else None
    run = canonicalize_scope_id(run_id, field="run_id") if run_id is not None else None
    thread = (
        canonicalize_scope_id(thread_id, field="thread_id") if thread_id is not None else None
    )
    now = _now()
    version = str(policy_version).strip() or "unspecified"
    grant = DestructiveGrant(
        grant_id=secrets.token_urlsafe(18),
        operation=op,
        object_type=otype,
        object_id=oid,
        issued_at=now,
        expires_at=now + float(expires_in),
        max_uses=max_uses,
        request_id=req,
        run_id=run,
        thread_id=thread,
        tenant=tenant_id,
        account=account_id,
        policy_version=version,
        policy_hash=policy_hash_for(
            operation=op,
            object_type=otype,
            object_id=oid,
            tenant=tenant_id,
            account=account_id,
            max_uses=max_uses,
            bind_request_id=bind_request_id,
            bind_run_id=bind_run_id,
            bind_thread_id=bind_thread_id,
            policy_version=version,
        ),
        provenance=PROVENANCE_HOST,
    )
    target = store if store is not None else get_destructive_grant_store()
    if target is None:
        raise RuntimeError(
            "No destructive grant store is bound. Call "
            "set_destructive_grant_store(...) or destructive_grants.bind(config) "
            "before issue_destructive_grant()."
        )
    target.put(grant)
    return grant


class DestructiveGrantIssuer:
    """Host-facing issuer. There is no model-facing approve/renew API."""

    def issue(self, **kwargs: Any) -> DestructiveGrant:
        return issue_destructive_grant(**kwargs)

    def bind(self, store_or_config: Any) -> DestructiveGrantStore:
        if hasattr(store_or_config, "build_destructive_grant_store"):
            store = store_or_config.build_destructive_grant_store()
        else:
            store = store_or_config
        set_destructive_grant_store(store)
        return store


destructive_grants = DestructiveGrantIssuer()


def _lookup_path(mapping: Mapping[str, Any], path: str) -> Any:
    current: Any = mapping
    for part in path.split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
        else:
            return _MISSING
    return current


def _split_bookkeeping(
    func: Callable[..., Any], kwargs: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    extra = {key: value for key, value in kwargs.items() if key in LEDGER_KWARG_KEYS}
    known = {key: value for key, value in kwargs.items() if key not in LEDGER_KWARG_KEYS}
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        return known, extra
    if any(
        param.kind is inspect.Parameter.VAR_KEYWORD
        for param in signature.parameters.values()
    ):
        return known, extra
    names = set(signature.parameters)
    for key, value in list(known.items()):
        if key not in names:
            extra[key] = value
            del known[key]
    return known, extra


def _bound_mapping(
    func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, Any]:
    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(*args, **kwargs)
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return dict(kwargs)


def _rebuild_call(
    func: Callable[..., Any],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    mapping: dict[str, Any],
    extra: dict[str, Any],
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    new_args = list(args)
    new_kwargs = dict(extra)
    try:
        signature = inspect.signature(func)
    except (TypeError, ValueError):
        new_kwargs.update(mapping)
        return tuple(new_args), new_kwargs
    positional = [
        param
        for param in signature.parameters.values()
        if param.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    for index, param in enumerate(positional):
        if param.name in mapping and index < len(new_args):
            new_args[index] = mapping[param.name]
        elif param.name in mapping:
            new_kwargs[param.name] = mapping[param.name]
    for key, value in mapping.items():
        if key not in {param.name for param in positional}:
            new_kwargs[key] = value
    return tuple(new_args), new_kwargs


def _scope_grants() -> tuple[DestructiveGrant, ...]:
    scope = get_active_execution_scope()
    if scope is None:
        return ()
    raw = getattr(scope, "destructive_grants", ()) or ()
    grants: list[DestructiveGrant] = []
    for item in raw:
        if isinstance(item, DestructiveGrant):
            grants.append(item)
    return tuple(grants)


def _call_request_id(kwargs: Mapping[str, Any]) -> str | None:
    value = kwargs.get("request_id")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _call_run_thread() -> tuple[str | None, str | None]:
    scope = get_active_execution_scope()
    if scope is None:
        return None, None
    run_id = scope.run_id or None
    thread_id = scope.thread_id or None
    return run_id, thread_id


def _safe_message(
    *,
    tool: str,
    operation: str,
    object_type: str,
    object_ref: str,
    reason: str,
) -> str:
    return (
        f"destructive grant {reason} for tool {tool!r} "
        f"operation={operation!r} object={object_type}/{object_ref}"
    )


def _set_decision(decision: DestructiveDecision) -> DestructiveDecision:
    _decision_var.set(decision)
    return decision


def _raise(
    *,
    tool: str,
    operation: str,
    object_type: str,
    object_ref: str,
    reason: str,
    decision: str,
    tenant: str | None,
    account: str | None,
    grant_ref: str | None,
    policy_version: str,
    request_id: str | None,
    run_id: str | None,
) -> None:
    _set_decision(
        DestructiveDecision(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            tenant=tenant,
            account=account,
            grant_ref=grant_ref,
            policy_version=policy_version,
            decision=decision,
            reason=reason,
            request_id=request_id,
            run_id=run_id,
            timestamp=_now(),
        )
    )
    raise DestructiveGrantError(
        _safe_message(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            reason=reason,
        ),
        tool=tool,
        operation=operation,
        object_type=object_type,
        object_ref=object_ref,
        reason=reason,
    )


def _grant_matches(
    grant: DestructiveGrant,
    *,
    operation: str,
    object_type: str,
    object_id: str,
    tenant: str | None,
    account: str | None,
    check_tenant: bool,
    check_account: bool,
    request_id: str | None,
    run_id: str | None,
    thread_id: str | None,
    spec: DestructiveGrantSpec,
    policy_version: str,
    now: float,
) -> str | None:
    """Return a mismatch reason, or None if the grant matches."""
    if grant.provenance != PROVENANCE_HOST:
        return REASON_UNVERIFIABLE
    if grant.operation != operation or grant.object_type != object_type:
        return REASON_MISMATCHED
    if grant.object_id != object_id:
        return REASON_MISMATCHED
    if check_tenant and (grant.tenant or None) != (tenant or None):
        return REASON_MISMATCHED
    if check_account and (grant.account or None) != (account or None):
        return REASON_MISMATCHED
    if policy_version != "unspecified" and grant.policy_version != policy_version:
        return REASON_MISMATCHED
    if spec.bind_request_id:
        if not request_id or grant.request_id != request_id:
            return REASON_MISMATCHED
    if spec.bind_run_id:
        if not run_id or grant.run_id != run_id:
            return REASON_MISMATCHED
    if spec.bind_thread_id:
        if not thread_id or grant.thread_id != thread_id:
            return REASON_MISMATCHED
    if now >= grant.expires_at:
        return REASON_EXPIRED
    return None


def enforce_destructive_confirm(
    tool: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    policy: DestructiveConfirmPolicy,
    func: Callable[..., Any] | None = None,
    store: DestructiveGrantStore | None = None,
) -> tuple[tuple[Any, ...], dict[str, Any], DestructiveDecision]:
    """Authorize a destructive call. Fail closed before any side effect."""
    tool_policy = policy.tools.get(tool)
    request_id = _call_request_id(kwargs)
    run_id, thread_id = _call_run_thread()
    if "run_id" in kwargs and isinstance(kwargs.get("run_id"), str) and kwargs["run_id"]:
        run_id = str(kwargs["run_id"])
    if "thread_id" in kwargs and isinstance(kwargs.get("thread_id"), str) and kwargs["thread_id"]:
        thread_id = str(kwargs["thread_id"])

    if tool_policy is None:
        decision = DestructiveDecision(
            tool=tool,
            operation="",
            object_type="",
            object_ref="",
            tenant=None,
            account=None,
            grant_ref=None,
            policy_version=policy.policy_version,
            decision=DECISION_ALLOWED,
            reason="undeclared",
            request_id=request_id,
            run_id=run_id,
            timestamp=_now(),
        )
        _set_decision(decision)
        return args, kwargs, decision

    tool_kwargs, extra = (
        _split_bookkeeping(func, kwargs) if func is not None else (dict(kwargs), {})
    )
    mapping = (
        _bound_mapping(func, args, tool_kwargs) if func is not None else dict(tool_kwargs)
    )
    spec = tool_policy.object
    try:
        operation = canonicalize_operation(tool_policy.operation)
        object_type = canonicalize_object_type(spec.object_type)
    except DestructiveCanonicalizeError as exc:
        _raise(
            tool=tool,
            operation=str(tool_policy.operation),
            object_type=str(spec.object_type),
            object_ref="",
            reason=REASON_MALFORMED,
            decision=DECISION_DENIED,
            tenant=None,
            account=None,
            grant_ref=None,
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )
        raise exc

    raw_id = _lookup_path(mapping, spec.id_from)
    tenant_raw = _lookup_path(mapping, spec.tenant_from) if spec.tenant_from else None
    account_raw = _lookup_path(mapping, spec.account_from) if spec.account_from else None
    if raw_id is _MISSING or raw_id in (None, "", [], ()):
        _raise(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref="",
            reason=REASON_MISSING,
            decision=DECISION_DENIED,
            tenant=None,
            account=None,
            grant_ref=None,
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )

    try:
        object_id = canonicalize_object_id(
            raw_id, object_type=object_type, case_sensitive=spec.case_sensitive
        )
        tenant = (
            canonicalize_scope_id(tenant_raw, field="tenant")
            if spec.tenant_from and tenant_raw not in (_MISSING, None)
            else None
        )
        account = (
            canonicalize_scope_id(account_raw, field="account")
            if spec.account_from and account_raw not in (_MISSING, None)
            else None
        )
        if spec.tenant_from and tenant is None:
            raise DestructiveCanonicalizeError("tenant is missing")
        if spec.account_from and account is None:
            raise DestructiveCanonicalizeError("account is missing")
    except DestructiveCanonicalizeError:
        _raise(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref="",
            reason=REASON_MALFORMED,
            decision=DECISION_DENIED,
            tenant=None,
            account=None,
            grant_ref=None,
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )

    object_ref = object_digest(object_type, object_id)
    now = _now()
    candidates = _scope_grants()
    matching: list[DestructiveGrant] = []
    drift = False
    expired_match = False
    for grant in candidates:
        reason = _grant_matches(
            grant,
            operation=operation,
            object_type=object_type,
            object_id=object_id,
            tenant=tenant,
            account=account,
            check_tenant=bool(spec.tenant_from),
            check_account=bool(spec.account_from),
            request_id=request_id,
            run_id=run_id,
            thread_id=thread_id,
            spec=tool_policy.grant,
            policy_version=policy.policy_version,
            now=now,
        )
        if reason is None:
            matching.append(grant)
        elif reason == REASON_EXPIRED and grant.object_id == object_id:
            expired_match = True
            # Already-consumed request_id may still RETURN a completed ledger
            # result without fresh authority (dedupe); include for try_consume.
            target_probe = store if store is not None else get_destructive_grant_store()
            if (
                request_id
                and target_probe is not None
                and tool_policy.grant.bind_request_id
            ):
                try:
                    rec = target_probe.get(grant.grant_id)
                except Exception:
                    rec = None
                consumed = (
                    [str(item) for item in (rec or {}).get("consumed_request_ids") or []]
                    if isinstance(rec, dict)
                    else []
                )
                if request_id in consumed:
                    matching.append(grant)
        elif (
            tool_policy.grant.bind_request_id
            and request_id
            and grant.request_id == request_id
            and (grant.object_id != object_id or grant.operation != operation)
        ):
            drift = True

    if not matching:
        if drift:
            _raise(
                tool=tool,
                operation=operation,
                object_type=object_type,
                object_ref=object_ref,
                reason=REASON_IDENTITY_DRIFT,
                decision=DECISION_MISMATCHED,
                tenant=tenant,
                account=account,
                grant_ref=None,
                policy_version=policy.policy_version,
                request_id=request_id,
                run_id=run_id,
            )
        if expired_match:
            _raise(
                tool=tool,
                operation=operation,
                object_type=object_type,
                object_ref=object_ref,
                reason=REASON_EXPIRED,
                decision=DECISION_EXPIRED,
                tenant=tenant,
                account=account,
                grant_ref=None,
                policy_version=policy.policy_version,
                request_id=request_id,
                run_id=run_id,
            )
        _raise(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            reason=REASON_MISSING if not candidates else REASON_MISMATCHED,
            decision=DECISION_DENIED if not candidates else DECISION_MISMATCHED,
            tenant=tenant,
            account=account,
            grant_ref=None,
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )

    target = store if store is not None else get_destructive_grant_store()
    if target is None:
        _raise(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            reason=REASON_UNVERIFIABLE,
            decision=DECISION_AMBIGUOUS,
            tenant=tenant,
            account=account,
            grant_ref=grant_digest(matching[0].grant_id),
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )

    consume_id = request_id or matching[0].grant_id
    try:
        result = target.try_consume(matching[0].grant_id, consume_id, now)
    except Exception:
        _raise(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            reason=REASON_STORAGE,
            decision=DECISION_DENIED,
            tenant=tenant,
            account=account,
            grant_ref=grant_digest(matching[0].grant_id),
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )

    grant_ref = grant_digest(matching[0].grant_id)
    if result.status == REASON_MISSING:
        _raise(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            reason=REASON_UNVERIFIABLE,
            decision=DECISION_AMBIGUOUS,
            tenant=tenant,
            account=account,
            grant_ref=grant_ref,
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )
    if result.status == REASON_EXPIRED:
        _raise(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            reason=REASON_EXPIRED,
            decision=DECISION_EXPIRED,
            tenant=tenant,
            account=account,
            grant_ref=grant_ref,
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )
    if result.status == REASON_EXHAUSTED:
        _raise(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            reason=REASON_EXHAUSTED,
            decision=DECISION_EXHAUSTED,
            tenant=tenant,
            account=account,
            grant_ref=grant_ref,
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )
    if result.status == DECISION_AMBIGUOUS:
        _raise(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            reason=REASON_UNVERIFIABLE,
            decision=DECISION_AMBIGUOUS,
            tenant=tenant,
            account=account,
            grant_ref=grant_ref,
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )
    if result.status not in {DECISION_ALLOWED, REASON_RETRY}:
        _raise(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            reason=REASON_MISMATCHED,
            decision=DECISION_MISMATCHED,
            tenant=tenant,
            account=account,
            grant_ref=grant_ref,
            policy_version=policy.policy_version,
            request_id=request_id,
            run_id=run_id,
        )

    decision = _set_decision(
        DestructiveDecision(
            tool=tool,
            operation=operation,
            object_type=object_type,
            object_ref=object_ref,
            tenant=tenant,
            account=account,
            grant_ref=grant_ref,
            policy_version=policy.policy_version,
            decision=DECISION_ALLOWED,
            reason=REASON_RETRY if result.status == REASON_RETRY else DECISION_ALLOWED,
            request_id=request_id,
            run_id=run_id,
            timestamp=now,
        )
    )
    # Register for mandatory use-phase expiry only when this call may still
    # execute. Ledger RETURN retries must not require fresh authority.
    if result.status != REASON_RETRY:
        grant_obj = result.grant or matching[0]
        from mycelium.authority_window import (
            AuthorityValidationPhase,
            bound_authority_from_destructive_grant,
            register_authority_for_use,
            validate_authority,
        )

        bound = bound_authority_from_destructive_grant(
            grant_obj,
            tool=tool,
            object_ref=object_ref,
            request_id=request_id,
            run_id=run_id,
        )
        validate_authority(
            bound,
            phase=AuthorityValidationPhase.AUTHORIZE,
            expected_policy_version=policy.policy_version,
        )
        register_authority_for_use(bound)
    if func is not None:
        return (*_rebuild_call(func, args, kwargs, mapping, extra), decision)
    merged = dict(mapping)
    merged.update(extra)
    return args, merged, decision


def sanitize_destructive_evidence(
    args: Sequence[Any],
    kwargs: Mapping[str, Any],
) -> tuple[list[Any], dict[str, Any]]:
    """Keep tool / operation / object type; drop destructive payload."""
    decision = get_active_destructive_decision()

    def scrub(value: Any, *, name: str | None = None, payload: bool = False) -> Any:
        key = (name or "").lower()
        if key in _PAYLOAD_FIELD_NAMES or payload:
            return PAYLOAD_OMITTED
        if decision is not None and name in {"object_id", "payment_id", "file_id"}:
            return decision.object_ref
        if isinstance(value, Mapping):
            return {
                str(k): scrub(
                    v,
                    name=str(k),
                    payload=str(k).lower() in _PAYLOAD_FIELD_NAMES,
                )
                for k, v in value.items()
            }
        if isinstance(value, list):
            return [scrub(item, payload=payload) for item in value]
        if isinstance(value, tuple):
            return [scrub(item, payload=payload) for item in value]
        return value

    safe_kwargs = scrub(dict(kwargs)) if isinstance(kwargs, Mapping) else {}
    assert isinstance(safe_kwargs, dict)
    return [scrub(item) for item in args], safe_kwargs


def destructive_fingerprint(decision: DestructiveDecision | None) -> tuple[str, ...]:
    if decision is None or decision.decision != DECISION_ALLOWED:
        return ()
    return (
        ":".join(
            [
                decision.operation,
                decision.object_type,
                decision.object_ref,
                decision.tenant or "",
                decision.account or "",
            ]
        ),
    )


def apply_destructive_confirm(
    func: Callable[P, R],
    policy: DestructiveConfirmPolicy,
    *,
    tool_name: str | None = None,
    store: DestructiveGrantStore | None = None,
    outcome_emitter: Any | None = None,
) -> Callable[P, R]:
    """Wrap *func* so a host grant is consumed before any inner guard or claim."""
    name = tool_name or getattr(func, "__name__", "tool")

    def _emit(decision: DestructiveDecision) -> None:
        if outcome_emitter is None:
            return
        try:
            outcome_emitter.emit_event(
                tool=decision.tool,
                request_id=decision.request_id or "",
                event="destructive_confirm",
                gate=decision.decision,
                resolution_reason=decision.reason,
                run_id=decision.run_id,
                policy_version=decision.policy_version,
                tool_body_executed=False,
            )
        except Exception:
            return

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            token = set_active_destructive_policy(policy)
            store_token = set_destructive_grant_store(store) if store is not None else None
            from mycelium.authority_window import clear_pending_authorities

            try:
                call_args, call_kwargs, decision = enforce_destructive_confirm(
                    name, args, kwargs, policy=policy, func=func, store=store
                )
                _emit(decision)
                if not getattr(func, "_mycelium_ledger", False):
                    from mycelium.authority_window import enforce_pending_authorities_at_use

                    enforce_pending_authorities_at_use()
                return await func(*call_args, **call_kwargs)
            except DestructiveGrantError as exc:
                current = get_active_destructive_decision()
                if current is not None:
                    _emit(current)
                else:
                    _emit(
                        DestructiveDecision(
                            tool=name,
                            operation=exc.operation or "",
                            object_type=exc.object_type or "",
                            object_ref=exc.object_ref or "",
                            tenant=None,
                            account=None,
                            grant_ref=None,
                            policy_version=policy.policy_version,
                            decision=DECISION_DENIED,
                            reason=exc.reason,
                            request_id=_call_request_id(kwargs),
                            run_id=_call_run_thread()[0],
                            timestamp=_now(),
                        )
                    )
                raise
            finally:
                clear_pending_authorities()
                reset_active_destructive_policy(token)
                if store_token is not None:
                    reset_destructive_grant_store(store_token)

        async_wrapper._mycelium_destructive_confirm = True  # type: ignore[attr-defined]
        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(func)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        token = set_active_destructive_policy(policy)
        store_token = set_destructive_grant_store(store) if store is not None else None
        from mycelium.authority_window import clear_pending_authorities

        try:
            call_args, call_kwargs, decision = enforce_destructive_confirm(
                name, args, kwargs, policy=policy, func=func, store=store
            )
            _emit(decision)
            if not getattr(func, "_mycelium_ledger", False):
                from mycelium.authority_window import enforce_pending_authorities_at_use

                enforce_pending_authorities_at_use()
            return func(*call_args, **call_kwargs)
        except DestructiveGrantError as exc:
            current = get_active_destructive_decision()
            if current is not None:
                _emit(current)
            else:
                _emit(
                    DestructiveDecision(
                        tool=name,
                        operation=exc.operation or "",
                        object_type=exc.object_type or "",
                        object_ref=exc.object_ref or "",
                        tenant=None,
                        account=None,
                        grant_ref=None,
                        policy_version=policy.policy_version,
                        decision=DECISION_DENIED,
                        reason=exc.reason,
                        request_id=_call_request_id(kwargs),
                        run_id=_call_run_thread()[0],
                        timestamp=_now(),
                    )
                )
            raise
        finally:
            clear_pending_authorities()
            reset_active_destructive_policy(token)
            if store_token is not None:
                reset_destructive_grant_store(store_token)

    sync_wrapper._mycelium_destructive_confirm = True  # type: ignore[attr-defined]
    return sync_wrapper  # type: ignore[return-value]


def destructive_confirm_policy_for_tool(
    policy: DestructiveConfirmPolicy, tool_name: str
) -> DestructiveConfirmPolicy:
    tool = policy.tools.get(tool_name)
    if tool is None:
        return replace(policy, tools={})
    return replace(policy, tools={tool_name: tool})


__all__ = [
    "DECISIONS",
    "DECISION_ALLOWED",
    "DECISION_AMBIGUOUS",
    "DECISION_DENIED",
    "DECISION_EXHAUSTED",
    "DECISION_EXPIRED",
    "DECISION_MISMATCHED",
    "DURABLE_STORAGES",
    "MISSING_POLICIES",
    "MISSING_POLICY_ERROR",
    "MISSING_POLICY_WARN",
    "SHARED_GRANT_STORAGES",
    "SINGLE_NODE_GRANT_STORAGES",
    "STORAGE_FILE",
    "STORAGE_MEMORY",
    "STORAGE_POSTGRES",
    "STORAGE_REDIS",
    "STORAGE_SQLITE",
    "ConsumeResult",
    "DestructiveConfirmPolicy",
    "DestructiveDecision",
    "DestructiveGrant",
    "DestructiveGrantError",
    "DestructiveGrantSpec",
    "DestructiveObjectSpec",
    "DestructiveToolPolicy",
    "FileDestructiveGrantStore",
    "InMemoryDestructiveGrantStore",
    "PostgresDestructiveGrantStore",
    "RedisDestructiveGrantStore",
    "SqliteDestructiveGrantStore",
    "apply_destructive_confirm",
    "canonicalize_object_id",
    "canonicalize_object_type",
    "canonicalize_operation",
    "destructive_confirm_policy_for_tool",
    "destructive_fingerprint",
    "destructive_grants",
    "enforce_destructive_confirm",
    "get_active_destructive_decision",
    "get_active_destructive_policy",
    "get_destructive_grant_store",
    "issue_destructive_grant",
    "register_destructive_object_canonicalizer",
    "registered_destructive_canonicalizers",
    "reset_destructive_confirm_state",
    "sanitize_destructive_evidence",
    "set_destructive_clock",
    "set_destructive_grant_store",
]
