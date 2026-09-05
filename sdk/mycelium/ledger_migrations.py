"""Explicit, idempotent migrations for durable ActionLedger rows."""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from mycelium.action_ledger import LEDGER_ENTRY_SCHEMA_VERSION, LedgerEntry
from mycelium.transition import TerminalOutcome


class LedgerMigrationError(Exception):
    """Raised when a ledger migration cannot be planned or applied safely."""


class LedgerMigrationStorage(Protocol):
    def get(self, request_id: str) -> LedgerEntry | None: ...

    def set(self, entry: LedgerEntry) -> None: ...

    def list_all(self) -> list[LedgerEntry]: ...


def _raw_schema_version(payload: dict[str, Any]) -> int:
    raw = payload.get("schema_version", 1)
    if isinstance(raw, bool):
        raise LedgerMigrationError("ledger schema_version must be an integer >= 1")
    if not isinstance(raw, (int, str)):
        raise LedgerMigrationError(
            f"ledger schema_version must be an integer, got {raw!r}"
        )
    try:
        version = int(raw)
    except (TypeError, ValueError) as exc:
        raise LedgerMigrationError(
            f"ledger schema_version must be an integer, got {raw!r}"
        ) from exc
    if version < 1:
        raise LedgerMigrationError(f"ledger schema_version must be >= 1, got {version}")
    return version


def _count_raw_versions(payloads: list[dict[str, Any]]) -> dict[int, int]:
    return dict(Counter(_raw_schema_version(payload) for payload in payloads))


def inspect_ledger_schema_versions(raw: dict[str, Any]) -> dict[int, int]:
    """Count raw envelope versions without initializing or modifying storage.

    This is the Doctor-facing path. Unlike normal storage readers it deliberately
    does not deserialize ``LedgerEntry`` objects, so unsupported future versions
    can be reported instead of raising before the count is known.
    """

    import json

    storage_type = str(raw.get("storage", "memory"))
    if storage_type == "memory":
        return {}
    if storage_type == "file":
        path = Path(str(raw.get("path") or ""))
        if not path.exists():
            return {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerMigrationError(f"cannot read file ledger schema: {exc}") from exc
        if not isinstance(loaded, dict):
            raise LedgerMigrationError("file ledger root must be a JSON object")
        payloads = [value for value in loaded.values() if isinstance(value, dict)]
        if len(payloads) != len(loaded):
            raise LedgerMigrationError("file ledger contains a non-object row")
        return _count_raw_versions(payloads)
    if storage_type == "sqlite":
        import sqlite3

        from mycelium.storage.sqlite_ledger import _validate_table_name

        path = Path(str(raw.get("path") or ""))
        if not path.exists():
            return {}
        table = _validate_table_name(str(raw.get("table", "mycelium_action_ledger")))
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if exists is None:
                return {}
            rows = conn.execute(f"SELECT payload FROM {table}").fetchall()  # nosec B608  # validated table name
        payloads = []
        for (payload,) in rows:
            try:
                decoded = payload if isinstance(payload, dict) else json.loads(payload)
            except (TypeError, json.JSONDecodeError) as exc:
                raise LedgerMigrationError(f"invalid SQLite ledger payload: {exc}") from exc
            if not isinstance(decoded, dict):
                raise LedgerMigrationError("SQLite ledger payload must be a JSON object")
            payloads.append(decoded)
        return _count_raw_versions(payloads)
    if storage_type == "redis":
        from mycelium.storage._helpers import resolve_storage_url
        from mycelium.storage.redis_ledger import _require_redis

        url = resolve_storage_url(raw)
        prefix = str(raw.get("prefix", "mycelium:action:"))
        redis = _require_redis()
        client = redis.Redis.from_url(url, decode_responses=True)
        payloads = []
        for key in client.scan_iter(match=f"{prefix}*"):
            value = client.get(key)
            if value is None:
                continue
            try:
                decoded = json.loads(value)
            except (TypeError, json.JSONDecodeError) as exc:
                raise LedgerMigrationError(f"invalid Redis ledger payload: {exc}") from exc
            if not isinstance(decoded, dict):
                raise LedgerMigrationError("Redis ledger payload must be a JSON object")
            payloads.append(decoded)
        return _count_raw_versions(payloads)
    if storage_type == "postgres":
        from mycelium.storage._helpers import resolve_storage_url
        from mycelium.storage.postgres_ledger import _require_psycopg, _validate_table_name

        dsn = resolve_storage_url(raw, url_key="dsn")
        table = _validate_table_name(str(raw.get("table", "mycelium_action_ledger")))
        psycopg, sql = _require_psycopg()
        with psycopg.connect(dsn) as conn:
            exists = conn.execute("SELECT to_regclass(%s)", (table,)).fetchone()
            if exists is None or exists[0] is None:
                return {}
            query = sql.SQL("SELECT payload FROM {}").format(sql.Identifier(table))
            rows = conn.execute(query).fetchall()
        payloads = []
        for (payload,) in rows:
            try:
                decoded = payload if isinstance(payload, dict) else json.loads(payload)
            except (TypeError, json.JSONDecodeError) as exc:
                raise LedgerMigrationError(f"invalid Postgres ledger payload: {exc}") from exc
            if not isinstance(decoded, dict):
                raise LedgerMigrationError("Postgres ledger payload must be a JSON object")
            payloads.append(decoded)
        return _count_raw_versions(payloads)
    raise LedgerMigrationError(f"unknown ledger storage type: {storage_type!r}")


@dataclass(frozen=True)
class LedgerMigrationPlan:
    target_version: int
    total_entries: int
    current_entries: int
    pending_entries: int
    active_pending_entries: int
    version_counts: dict[int, int]
    unsupported_versions: tuple[int, ...] = ()

    @property
    def can_apply(self) -> bool:
        return not self.unsupported_versions

    def to_dict(self) -> dict[str, object]:
        return {
            "target_version": self.target_version,
            "total_entries": self.total_entries,
            "current_entries": self.current_entries,
            "pending_entries": self.pending_entries,
            "active_pending_entries": self.active_pending_entries,
            "version_counts": {
                str(version): count for version, count in sorted(self.version_counts.items())
            },
            "unsupported_versions": list(self.unsupported_versions),
            "can_apply": self.can_apply,
        }


@dataclass(frozen=True)
class LedgerMigrationResult:
    target_version: int
    migrated_entries: int
    unchanged_entries: int

    def to_dict(self) -> dict[str, int]:
        return {
            "target_version": self.target_version,
            "migrated_entries": self.migrated_entries,
            "unchanged_entries": self.unchanged_entries,
        }


def _validated_target(target_version: int) -> int:
    if target_version != LEDGER_ENTRY_SCHEMA_VERSION:
        if target_version < LEDGER_ENTRY_SCHEMA_VERSION:
            raise LedgerMigrationError(
                "ledger downgrades are not supported; restore the pre-migration backup "
                "with the older Mycelium version instead"
            )
        raise LedgerMigrationError(
            f"target schema {target_version} is newer than this runtime supports "
            f"({LEDGER_ENTRY_SCHEMA_VERSION})"
        )
    return target_version


def _entry_version(entry: LedgerEntry) -> int:
    version = int(entry.schema_version)
    if version < 1:
        raise LedgerMigrationError(
            f"entry {entry.request_id!r} has invalid schema_version {version}"
        )
    return version


def _upgrade_v1_to_v2(entry: LedgerEntry) -> LedgerEntry:
    # Schema 1 predates durable effect identity. Its only safe identity is the
    # physical request id, which is also LedgerEntry.from_dict's compatibility
    # behavior. Preserve any partially populated values.
    effect_id = str(entry.effect_id or entry.request_id)
    aliases = tuple(dict.fromkeys((*entry.request_id_aliases, entry.request_id)))
    return replace(
        entry,
        effect_id=effect_id,
        request_id_aliases=aliases,
        schema_version=2,
    )


_UPGRADES = {1: _upgrade_v1_to_v2}


def upgrade_ledger_entry(
    entry: LedgerEntry,
    *,
    target_version: int = LEDGER_ENTRY_SCHEMA_VERSION,
) -> LedgerEntry:
    """Return ``entry`` upgraded to ``target_version`` without writing it."""

    target = _validated_target(target_version)
    version = _entry_version(entry)
    if version > target:
        raise LedgerMigrationError(
            f"entry {entry.request_id!r} uses unsupported future schema {version}"
        )
    upgraded = entry
    while version < target:
        migration = _UPGRADES.get(version)
        if migration is None:
            raise LedgerMigrationError(
                f"no ledger migration registered from schema {version} to {version + 1}"
            )
        upgraded = migration(upgraded)
        version = _entry_version(upgraded)
    return upgraded


def plan_ledger_migration(
    storage: LedgerMigrationStorage,
    *,
    target_version: int = LEDGER_ENTRY_SCHEMA_VERSION,
    now: float | None = None,
) -> LedgerMigrationPlan:
    """Inspect a ledger without modifying rows and return a migration plan."""

    target = _validated_target(target_version)
    observed_at = time.time() if now is None else now
    entries = storage.list_all()
    versions: Counter[int] = Counter()
    pending = 0
    active_pending = 0
    unsupported: set[int] = set()
    for entry in entries:
        version = _entry_version(entry)
        versions[version] += 1
        if version > target:
            unsupported.add(version)
            continue
        if version < target:
            upgrade_ledger_entry(entry, target_version=target)
            pending += 1
            if entry.resolved_terminal_outcome(now=observed_at) == TerminalOutcome.IN_FLIGHT:
                active_pending += 1
    return LedgerMigrationPlan(
        target_version=target,
        total_entries=len(entries),
        current_entries=versions.get(target, 0),
        pending_entries=pending,
        active_pending_entries=active_pending,
        version_counts=dict(versions),
        unsupported_versions=tuple(sorted(unsupported)),
    )


def apply_ledger_migration(
    storage: LedgerMigrationStorage,
    *,
    target_version: int = LEDGER_ENTRY_SCHEMA_VERSION,
    allow_active: bool = False,
) -> LedgerMigrationResult:
    """Upgrade every older row, refusing active or unsupported rows by default."""

    plan = plan_ledger_migration(storage, target_version=target_version)
    if not plan.can_apply:
        raise LedgerMigrationError(
            f"ledger contains unsupported schema versions {list(plan.unsupported_versions)}"
        )
    if plan.active_pending_entries and not allow_active:
        raise LedgerMigrationError(
            f"{plan.active_pending_entries} migration candidate(s) are IN_FLIGHT; "
            "stop workers first, then pass --allow-active only after confirming "
            "no worker can still write those rows"
        )

    migrated = 0
    unchanged = 0
    for entry in storage.list_all():
        if _entry_version(entry) == plan.target_version:
            unchanged += 1
            continue
        upgraded = upgrade_ledger_entry(entry, target_version=plan.target_version)
        storage.set(upgraded)
        stored = storage.get(entry.request_id)
        if stored is None or _entry_version(stored) != plan.target_version:
            raise LedgerMigrationError(
                f"migration write verification failed for entry {entry.request_id!r}"
            )
        migrated += 1
    return LedgerMigrationResult(
        target_version=plan.target_version,
        migrated_entries=migrated,
        unchanged_entries=unchanged,
    )


__all__ = [
    "LedgerMigrationError",
    "LedgerMigrationPlan",
    "LedgerMigrationResult",
    "LedgerMigrationStorage",
    "apply_ledger_migration",
    "inspect_ledger_schema_versions",
    "plan_ledger_migration",
    "upgrade_ledger_entry",
]
