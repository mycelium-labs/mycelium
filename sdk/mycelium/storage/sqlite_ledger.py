"""SQLite-backed ledger storage (stdlib sqlite3; zero-ops single-node)."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from mycelium.storage._helpers import ClaimOutcome, claim_inflight_outcome, with_lease

E = TypeVar("E")

_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_table_name(table: str) -> str:
    if not _TABLE_RE.fullmatch(table):
        raise ValueError(
            f"invalid SQLite table name {table!r}; use lowercase letters, digits, underscores"
        )
    return table


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

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_ready:
            return
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._table} ("
            "request_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        conn.commit()
        self._schema_ready = True

    def get(self, request_id: str) -> E | None:
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                row = conn.execute(
                    f"SELECT payload FROM {self._table} WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
        if row is None:
            return None
        return self._from_dict(_payload_dict(row["payload"]))

    def set(self, entry: E) -> None:
        payload = json.dumps(entry.to_dict(), default=str)
        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                conn.execute(
                    f"INSERT INTO {self._table} (request_id, payload) VALUES (?, ?) "
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
        leased = with_lease(entry, now=now, lease_ttl=lease_ttl)
        payload = json.dumps(leased.to_dict(), default=str)

        with self._lock:
            with self._connect() as conn:
                self._ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(
                        f"INSERT OR IGNORE INTO {self._table} (request_id, payload) "
                        "VALUES (?, ?)",
                        (entry.request_id, payload),
                    )
                    if cur.rowcount == 1:
                        conn.commit()
                        return "claimed", None

                    row = conn.execute(
                        f"SELECT payload FROM {self._table} WHERE request_id = ?",
                        (entry.request_id,),
                    ).fetchone()
                    if row is None:
                        # Rare race: deleted between IGNORE miss and SELECT.
                        conn.execute(
                            f"INSERT INTO {self._table} (request_id, payload) "
                            "VALUES (?, ?)",
                            (entry.request_id, payload),
                        )
                        conn.commit()
                        return "claimed", None

                    existing = self._from_dict(_payload_dict(row["payload"]))
                    outcome = claim_inflight_outcome(existing, now=now)
                    if outcome == "completed":
                        conn.commit()
                        return "completed", existing
                    if outcome == "in_flight":
                        conn.commit()
                        return "in_flight", existing

                    conn.execute(
                        f"UPDATE {self._table} SET payload = ? WHERE request_id = ?",
                        (payload, entry.request_id),
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
    ) -> bool:
        if not expected_terminal_outcomes:
            return False
        payload = json.dumps(entry.to_dict(), default=str)
        outcomes = sorted(expected_terminal_outcomes)
        placeholders = ", ".join("?" for _ in outcomes)
        sql = (
            f"UPDATE {self._table} SET payload = ? "
            f"WHERE request_id = ? "
            f"AND json_extract(payload, '$.terminal_outcome') IN ({placeholders})"
        )
        params: list[Any] = [payload, entry.request_id, *outcomes]
        if expected_owner is not None:
            sql += " AND json_extract(payload, '$.owner') = ?"
            params.append(expected_owner)

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
                rows = conn.execute(f"SELECT payload FROM {self._table}").fetchall()
        return [self._from_dict(_payload_dict(row["payload"])) for row in rows]


class SqliteLedgerStorage:
    """SQLite storage for :class:`~mycelium.action_ledger.LedgerEntry`."""

    def __init__(
        self,
        path: str | Path,
        *,
        table: str = "mycelium_action_ledger",
    ) -> None:
        from mycelium.action_ledger import LedgerEntry

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

    def try_transition(
        self,
        entry: Any,
        *,
        expected_terminal_outcomes: frozenset[str],
        expected_owner: str | None = None,
    ) -> bool:
        return self._inner.try_transition(
            entry,
            expected_terminal_outcomes=expected_terminal_outcomes,
            expected_owner=expected_owner,
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
