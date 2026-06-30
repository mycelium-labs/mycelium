"""Postgres-backed ledger storage with INSERT ... ON CONFLICT claim semantics."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any, TypeVar

from mycelium.storage._helpers import ClaimOutcome

E = TypeVar("E")

_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_table_name(table: str) -> str:
    if not _TABLE_RE.fullmatch(table):
        raise ValueError(
            f"invalid Postgres table name {table!r}; use lowercase letters, digits, underscores"
        )
    return table


def _require_psycopg() -> Any:
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise ImportError(
            "Postgres storage requires the 'psycopg' package. "
            "Install with: pip install 'mycelium-runtime[postgres]'"
        ) from exc
    return psycopg, sql


def _payload_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    return dict(json.loads(raw))


class PostgresEntryStorage:
    """Generic Postgres table store for ledger entries keyed by request_id."""

    def __init__(
        self,
        dsn: str,
        *,
        table: str,
        from_dict: Callable[[dict[str, Any]], E],
    ) -> None:
        psycopg, sql = _require_psycopg()
        self._psycopg = psycopg
        self._sql = sql
        self._dsn = dsn
        self._table = _validate_table_name(table)
        self._from_dict = from_dict
        self._schema_ready = False

    def _table_id(self) -> Any:
        return self._sql.Identifier(self._table)

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        query = self._sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} ("
            "request_id TEXT PRIMARY KEY, payload JSONB NOT NULL)"
        ).format(self._table_id())
        with self._psycopg.connect(self._dsn) as conn:
            conn.execute(query)
            conn.commit()
        self._schema_ready = True

    def get(self, request_id: str) -> E | None:
        self._ensure_schema()
        query = self._sql.SQL(
            "SELECT payload FROM {} WHERE request_id = %s"
        ).format(self._table_id())
        with self._psycopg.connect(self._dsn) as conn:
            row = conn.execute(query, (request_id,)).fetchone()
        if row is None:
            return None
        return self._from_dict(_payload_dict(row[0]))

    def set(self, entry: E) -> None:
        self._ensure_schema()
        payload = json.loads(json.dumps(entry.to_dict(), default=str))
        query = self._sql.SQL(
            "INSERT INTO {} (request_id, payload) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (request_id) DO UPDATE SET payload = EXCLUDED.payload"
        ).format(self._table_id())
        with self._psycopg.connect(self._dsn) as conn:
            conn.execute(query, (entry.request_id, json.dumps(payload)))
            conn.commit()

    def try_claim_inflight(self, entry: E) -> tuple[ClaimOutcome, E | None]:
        self._ensure_schema()
        payload = json.loads(json.dumps(entry.to_dict(), default=str))
        insert_query = self._sql.SQL(
            "INSERT INTO {} (request_id, payload) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (request_id) DO NOTHING RETURNING request_id"
        ).format(self._table_id())
        select_for_update = self._sql.SQL(
            "SELECT payload FROM {} WHERE request_id = %s FOR UPDATE"
        ).format(self._table_id())
        update_failed = self._sql.SQL(
            "UPDATE {} SET payload = %s::jsonb "
            "WHERE request_id = %s AND payload->>'status' = 'failed' "
            "RETURNING request_id"
        ).format(self._table_id())
        select_one = self._sql.SQL(
            "SELECT payload FROM {} WHERE request_id = %s"
        ).format(self._table_id())

        with self._psycopg.connect(self._dsn) as conn:
            with conn.transaction():
                inserted = conn.execute(
                    insert_query,
                    (entry.request_id, json.dumps(payload)),
                ).fetchone()
                if inserted is not None:
                    return "claimed", None

                row = conn.execute(select_for_update, (entry.request_id,)).fetchone()
                if row is None:
                    return "claimed", None

                existing = self._from_dict(_payload_dict(row[0]))
                if existing.status == "completed":
                    return "completed", existing
                if existing.status == "in-flight":
                    return "in_flight", existing

                updated = conn.execute(
                    update_failed,
                    (json.dumps(payload), entry.request_id),
                ).fetchone()
                if updated is not None:
                    return "claimed", None

                row = conn.execute(select_one, (entry.request_id,)).fetchone()
                if row is None:
                    return "claimed", None
                existing = self._from_dict(_payload_dict(row[0]))
                if existing.status == "completed":
                    return "completed", existing
                return "in_flight", existing

    def list_all(self) -> list[E]:
        self._ensure_schema()
        query = self._sql.SQL("SELECT payload FROM {}").format(self._table_id())
        with self._psycopg.connect(self._dsn) as conn:
            rows = conn.execute(query).fetchall()
        return [self._from_dict(_payload_dict(row[0])) for row in rows]


class PostgresLedgerStorage:
    """Postgres storage for :class:`~mycelium.action_ledger.LedgerEntry`."""

    def __init__(
        self,
        dsn: str,
        *,
        table: str = "mycelium_action_ledger",
    ) -> None:
        from mycelium.action_ledger import LedgerEntry

        self._inner = PostgresEntryStorage(
            dsn,
            table=table,
            from_dict=LedgerEntry.from_dict,
        )

    def get(self, request_id: str) -> Any:
        return self._inner.get(request_id)

    def set(self, entry: Any) -> None:
        self._inner.set(entry)

    def try_claim_inflight(self, entry: Any) -> tuple[ClaimOutcome, Any | None]:
        return self._inner.try_claim_inflight(entry)

    def list_all(self) -> list[Any]:
        return self._inner.list_all()


class PostgresTaskLedgerStorage:
    """Postgres storage for :class:`~mycelium.task_ledger.TaskLedgerEntry`."""

    def __init__(
        self,
        dsn: str,
        *,
        table: str = "mycelium_task_ledger",
    ) -> None:
        from mycelium.task_ledger import TaskLedgerEntry

        self._inner = PostgresEntryStorage(
            dsn,
            table=table,
            from_dict=TaskLedgerEntry.from_dict,
        )

    def get(self, request_id: str) -> Any:
        return self._inner.get(request_id)

    def set(self, entry: Any) -> None:
        self._inner.set(entry)

    def try_claim_inflight(self, entry: Any) -> tuple[ClaimOutcome, Any | None]:
        return self._inner.try_claim_inflight(entry)

    def list_all(self) -> list[Any]:
        return self._inner.list_all()
