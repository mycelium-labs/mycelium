"""SQLite-backed ledger storage (stdlib sqlite3; zero-ops single-node)."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any, TypeVar

from mycelium.storage._helpers import ClaimOutcome, claim_inflight_outcome, with_lease

E = TypeVar("E")

_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


# In SQLite, table names cannot be parameterized with `?` placeholders.
# All table names are strictly validated against `^[a-z][a-z0-9_]*$` upon
# initialization to prevent SQL injection before dynamic string formatting into queries.
def _validate_table_name(table: str) -> str:
    if not _TABLE_RE.fullmatch(table):
        raise ValueError(
            f"invalid SQLite table name {table!r}; use lowercase letters, digits, underscores"
        )
    return table  # nosec B608


def _payload_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    return dict(json.loads(raw))


class SqliteEntryStorage:
    """Generic SQLite table store for ledger entries keyed by request_id.

    Mirrors :class:`~mycelium.storage.postgres_ledger.PostgresEntryStorage`:
    ``request_id`` + JSON ``payload``, transactional claim / CAS transition.
    Uses stdlib ``sqlite3`` + WAL; no Redis TTL (leases live in payload).
    """

    def __init__(
        self,
        path: str | Path,
        *,
        table: str,
        from_dict: Callable[[dict[str, Any]], E],
    ) -> None:
        self._path = Path(path)
        self._table = _validate_table_name(table)
        self._from_dict = from_dict
        self._lock = threading.Lock()
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self._path),
            timeout=30.0,
            check_same_thread=False,
        )
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _effect_id_for_entry(entry: Any) -> str:
        return str(getattr(entry, "effect_id", None) or entry.request_id)

    @staticmethod
    def _effect_id_from_row(request_id: str, payload: dict[str, Any]) -> str:
        return str(payload.get("effect_id") or request_id)

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "request_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        conn.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {self._table}_effect_id_unique "
            f"ON {self._table} (COALESCE(json_extract(payload, '$.effect_id'), request_id))"
        )
        conn.commit()
        self._schema_ready = True

    def get(self, request_id: str) -> E | None:
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    f"SELECT payload FROM {self._table} WHERE request_id = ?",  # nosec B608
                    (request_id,),
                ).fetchone()
        if row is None:
            return None
        return self._from_dict(_payload_dict(row["payload"]))

    def set(self, entry: E) -> None:
        payload = json.dumps(entry.to_dict(), default=str)
        effect_id = self._effect_id_for_entry(entry)
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                existing = conn.execute(
                    f"SELECT request_id FROM {self._table} "  # nosec B608
                    "WHERE COALESCE(json_extract(payload, '$.effect_id'), request_id) = ? "
                    "LIMIT 1",
                    (effect_id,),
                ).fetchone()
                if existing is not None and str(existing["request_id"]) != entry.request_id:
                    conn.commit()
                    return
                conn.execute(
                    f"INSERT INTO {self._table} (request_id, payload) VALUES (?, ?) "  # nosec B608
                    "ON CONFLICT(request_id) DO UPDATE SET payload = excluded.payload",
                    (entry.request_id, payload),
                )
                conn.commit()

    def try_claim_inflight(
        self,
        entry: E,
        *,
        lease_ttl: float = 3600.0,
    ) -> tuple[ClaimOutcome, E | None]:
        now = time.time()
        effect_id = self._effect_id_for_entry(entry)
        fresh_payload = json.dumps(
            with_lease(entry, now=now, lease_ttl=lease_ttl).to_dict(), default=str
        )

        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(
                        f"INSERT OR IGNORE INTO {self._table} (request_id, payload) VALUES (?, ?)",  # nosec B608
                        (entry.request_id, fresh_payload),
                    )
                    if cur.rowcount == 1:
                        conn.commit()
                        return "claimed", None

                    row = conn.execute(
                        f"SELECT request_id, payload FROM {self._table} WHERE request_id = ?",  # nosec B608
                        (entry.request_id,),
                    ).fetchone()
                    if row is None:
                        row = conn.execute(
                            f"SELECT request_id, payload FROM {self._table} "  # nosec B608
                            "WHERE COALESCE(json_extract(payload, '$.effect_id'), request_id) = ? "
                            "ORDER BY request_id LIMIT 1",
                            (effect_id,),
                        ).fetchone()
                    if row is None:
                        # Rare race: row vanished between IGNORE miss and lookup.
                        inserted = conn.execute(
                            f"INSERT OR IGNORE INTO {self._table} "  # nosec B608
                            "(request_id, payload) VALUES (?, ?)",
                            (entry.request_id, fresh_payload),
                        )
                        if inserted.rowcount == 1:
                            conn.commit()
                            return "claimed", None
                        row = conn.execute(
                            f"SELECT request_id, payload FROM {self._table} "  # nosec B608
                            "WHERE COALESCE(json_extract(payload, '$.effect_id'), request_id) = ? "
                            "ORDER BY request_id LIMIT 1",
                            (effect_id,),
                        ).fetchone()
                        if row is None:
                            conn.commit()
                            return "in_flight", None

                    active_request_id = str(row["request_id"])
                    existing = self._from_dict(_payload_dict(row["payload"]))
                    outcome = claim_inflight_outcome(existing, now=now)
                    if outcome == "completed":
                        conn.commit()
                        return "completed", existing
                    if outcome == "in_flight":
                        conn.commit()
                        return "in_flight", existing

                    claim_entry = (
                        entry
                        if active_request_id == entry.request_id
                        else replace(entry, request_id=active_request_id)
                    )
                    reclaimed = with_lease(
                        claim_entry, now=now, lease_ttl=lease_ttl, prior=existing
                    )
                    conn.execute(
                        f"UPDATE {self._table} SET payload = ? WHERE request_id = ?",  # nosec B608
                        (json.dumps(reclaimed.to_dict(), default=str), active_request_id),
                    )
                    conn.commit()
                    return "claimed", None
                except Exception:
                    conn.rollback()
                    raise

    def try_transition(
        self,
        entry: E,
        *,
        expected_terminal_outcomes: frozenset[str],
        expected_owner: str | None = None,
        require_lease_held_at: float | None = None,
        expected_fence: int | None = None,
        expected_effect_state: str | None = None,
    ) -> bool:
        if not expected_terminal_outcomes:
            return False
        payload = json.dumps(entry.to_dict(), default=str)
        outcomes = sorted(expected_terminal_outcomes)
        placeholders = ", ".join("?" for _ in outcomes)
        sql = (
            f"UPDATE {self._table} SET payload = ? "  # nosec B608
            f"WHERE request_id = ? "
            f"AND json_extract(payload, '$.terminal_outcome') IN ({placeholders})"
        )
        params: list[Any] = [payload, entry.request_id, *outcomes]
        if expected_owner is not None:
            sql += " AND json_extract(payload, '$.owner') = ?"
            params.append(expected_owner)
        if expected_fence is not None:
            # COALESCE so old rows (payload without a fence) read as 0.
            sql += " AND COALESCE(json_extract(payload, '$.fence'), 0) = ?"
            params.append(expected_fence)
        if expected_effect_state is not None:
            sql += " AND COALESCE(json_extract(payload, '$.effect_phase'), 'INTENDED') = ?"
            params.append(expected_effect_state)
        if require_lease_held_at is not None:
            sql += (
                " AND (json_extract(payload, '$.lease_until') IS NULL "
                "OR json_extract(payload, '$.lease_until') > ?)"
            )
            params.append(require_lease_held_at)

        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                cur = conn.execute(sql, params)
                conn.commit()
                return cur.rowcount == 1

    def list_all(self) -> list[E]:
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                rows = conn.execute(f"SELECT payload FROM {self._table}").fetchall()  # nosec B608
        return [self._from_dict(_payload_dict(row["payload"])) for row in rows]

    def delete_entries(self, request_ids: list[str]) -> int:
        if not request_ids:
            return 0
        placeholders = ", ".join("?" for _ in request_ids)
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                result = conn.execute(
                    f"DELETE FROM {self._table} WHERE request_id IN ({placeholders})",  # nosec B608
                    request_ids,
                )
                conn.commit()
                return int(result.rowcount)

    def resolve_request_id(self, effect_id: str) -> str | None:
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    f"SELECT request_id FROM {self._table} "  # nosec B608
                    "WHERE COALESCE(json_extract(payload, '$.effect_id'), request_id) = ? "
                    "ORDER BY request_id LIMIT 1",
                    (effect_id,),
                ).fetchone()
                if row is not None:
                    return str(row["request_id"])
                rows = conn.execute(f"SELECT request_id, payload FROM {self._table}").fetchall()  # nosec B608
        candidates: list[tuple[float, str]] = []
        for row in rows:
            request_id = str(row["request_id"])
            payload = _payload_dict(row["payload"])
            if self._effect_id_from_row(request_id, payload) == effect_id:
                started = payload.get("started_at")
                try:
                    started_at = float(started) if started is not None else 0.0
                except (TypeError, ValueError):
                    started_at = 0.0
                candidates.append((started_at, request_id))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][1]

    def get_by_effect_id(self, effect_id: str) -> E | None:
        request_id = self.resolve_request_id(effect_id)
        if request_id is None:
            return None
        return self.get(request_id)


class SqliteLedgerStorage:
    """SQLite storage for :class:`~mycelium.action_ledger.LedgerEntry`."""

    def __init__(
        self,
        path: str | Path,
        *,
        table: str = "mycelium_action_ledger",
    ) -> None:
        from mycelium.ledger_model import LedgerEntry

        self._inner = SqliteEntryStorage(
            path,
            table=table,
            from_dict=LedgerEntry.from_dict,
        )

    def get(self, request_id: str) -> Any:
        return self._inner.get(request_id)

    def set(self, entry: Any) -> None:
        self._inner.set(entry)

    def try_claim_inflight(
        self,
        entry: Any,
        *,
        lease_ttl: float = 3600.0,
    ) -> tuple[ClaimOutcome, Any | None]:
        return self._inner.try_claim_inflight(entry, lease_ttl=lease_ttl)

    def list_all(self) -> list[Any]:
        return self._inner.list_all()

    def delete_entries(self, request_ids: list[str]) -> int:
        return self._inner.delete_entries(request_ids)

    def resolve_request_id(self, effect_id: str) -> str | None:
        return self._inner.resolve_request_id(effect_id)

    def get_by_effect_id(self, effect_id: str) -> Any | None:
        return self._inner.get_by_effect_id(effect_id)

    def try_transition(
        self,
        entry: Any,
        *,
        expected_terminal_outcomes: frozenset[str],
        expected_owner: str | None = None,
        require_lease_held_at: float | None = None,
        expected_fence: int | None = None,
        expected_effect_state: str | None = None,
    ) -> bool:
        return self._inner.try_transition(
            entry,
            expected_terminal_outcomes=expected_terminal_outcomes,
            expected_owner=expected_owner,
            require_lease_held_at=require_lease_held_at,
            expected_fence=expected_fence,
            expected_effect_state=expected_effect_state,
        )


class SqliteTaskLedgerStorage:
    """SQLite storage for :class:`~mycelium.task_ledger.TaskLedgerEntry`."""

    def __init__(
        self,
        path: str | Path,
        *,
        table: str = "mycelium_task_ledger",
    ) -> None:
        from mycelium.task_ledger import TaskLedgerEntry

        self._inner = SqliteEntryStorage(
            path,
            table=table,
            from_dict=TaskLedgerEntry.from_dict,
        )

    def get(self, request_id: str) -> Any:
        return self._inner.get(request_id)

    def set(self, entry: Any) -> None:
        self._inner.set(entry)

    def try_claim_inflight(
        self,
        entry: Any,
        *,
        lease_ttl: float = 3600.0,
    ) -> tuple[ClaimOutcome, Any | None]:
        return self._inner.try_claim_inflight(entry, lease_ttl=lease_ttl)

    def list_all(self) -> list[Any]:
        return self._inner.list_all()

    def resolve_request_id(self, effect_id: str) -> str | None:
        return self._inner.resolve_request_id(effect_id)

    def get_by_effect_id(self, effect_id: str) -> Any | None:
        return self._inner.get_by_effect_id(effect_id)
