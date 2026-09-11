"""Postgres-backed append-only outcome storage."""

from __future__ import annotations

import json
import re
from typing import Any

from mycelium.outcome_emit import OutcomeRow, OutcomeStorage
from mycelium.storage._helpers import redact_secrets

_TABLE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _validate_table_name(table: str) -> str:
    if not _TABLE_RE.fullmatch(table):
        raise ValueError(
            f"invalid Postgres table name {table!r}; "
            "use lowercase letters, digits, underscores"
        )
    return table


def _require_psycopg() -> Any:
    try:
        import psycopg
        from psycopg import sql
    except ImportError as exc:
        raise ImportError(
            "Postgres outcome storage requires the 'psycopg' package. "
            "Install with: pip install 'mycelium-runtime[postgres]'"
        ) from exc
    return psycopg, sql


def _payload_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    return dict(json.loads(raw))


class PostgresOutcomeStorage(OutcomeStorage):
    """Append-only Postgres store for :class:`~mycelium.outcome_emit.OutcomeRow`.

    Rows are keyed by ``event_id`` so a retried insert is idempotent. Indexed
    columns support reporting filters; the full row lives in ``payload``.
    """

    def __init__(
        self,
        dsn: str,
        *,
        table: str = "mycelium_outcomes",
    ) -> None:
        psycopg, sql = _require_psycopg()
        self._psycopg = psycopg
        self._sql = sql
        self._dsn = dsn
        self._table = _validate_table_name(table)
        self._schema_ready = False

    def _table_id(self) -> Any:
        return self._sql.Identifier(self._table)

    def _index_id(self, suffix: str) -> Any:
        return self._sql.Identifier(f"{self._table}_{suffix}")

    def _wrap_error(self, exc: BaseException, *, action: str) -> RuntimeError:
        detail = redact_secrets(str(exc))
        return RuntimeError(
            f"Postgres outcome storage failed during {action} "
            f"(table={self._table!r}): {detail}. "
            "Check connectivity, schema permissions (CREATE/INSERT/SELECT), "
            "and that the DSN is reachable."
        )

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        table = self._table_id()
        statements = [
            self._sql.SQL(
                "CREATE TABLE IF NOT EXISTS {} ("
                "event_id TEXT PRIMARY KEY, "
                "ts DOUBLE PRECISION NOT NULL, "
                "request_id TEXT NOT NULL, "
                "run_id TEXT, "
                "tool TEXT NOT NULL, "
                "terminal_outcome TEXT, "
                "payload JSONB NOT NULL)"
            ).format(table),
            self._sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} (request_id)"
            ).format(self._index_id("request_id_idx"), table),
            self._sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} (run_id)"
            ).format(self._index_id("run_id_idx"), table),
            self._sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} (ts)"
            ).format(self._index_id("ts_idx"), table),
            self._sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} (tool)"
            ).format(self._index_id("tool_idx"), table),
            self._sql.SQL(
                "CREATE INDEX IF NOT EXISTS {} ON {} (terminal_outcome)"
            ).format(self._index_id("terminal_outcome_idx"), table),
        ]
        try:
            with self._psycopg.connect(self._dsn) as conn:
                conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"mycelium:schema:outcome:{self._table}",),
                )
                for statement in statements:
                    conn.execute(statement)
                conn.commit()
        except Exception as exc:
            raise self._wrap_error(exc, action="schema ensure") from exc
        self._schema_ready = True

    def validate(self) -> None:
        """Initialize the schema and prove the configured database is usable."""
        try:
            self._ensure_schema()
            with self._psycopg.connect(self._dsn, connect_timeout=5) as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as exc:
            detail = redact_secrets(str(exc))
            raise RuntimeError(
                "Postgres outcome storage is not ready: "
                f"{type(exc).__name__}: {detail}"
            ) from None

    def append(self, row: OutcomeRow) -> None:
        if not row.event_id:
            raise ValueError(
                "PostgresOutcomeStorage.append requires OutcomeRow.event_id "
                "(OutcomeEmitter mints one automatically)"
            )
        self._ensure_schema()
        payload = json.loads(json.dumps(row.to_dict(), default=str))
        query = self._sql.SQL(
            "INSERT INTO {} ("
            "event_id, ts, request_id, run_id, tool, terminal_outcome, payload"
            ") VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (event_id) DO NOTHING"
        ).format(self._table_id())
        try:
            with self._psycopg.connect(self._dsn) as conn:
                conn.execute(
                    query,
                    (
                        row.event_id,
                        float(row.ts),
                        row.request_id,
                        row.run_id,
                        row.tool,
                        row.terminal_outcome,
                        json.dumps(payload),
                    ),
                )
                conn.commit()
        except Exception as exc:
            raise self._wrap_error(exc, action="append") from exc

    def list_all(self) -> list[OutcomeRow]:
        self._ensure_schema()
        query = self._sql.SQL(
            "SELECT payload FROM {} ORDER BY ts ASC, event_id ASC"
        ).format(self._table_id())
        try:
            with self._psycopg.connect(self._dsn) as conn:
                rows = conn.execute(query).fetchall()
        except Exception as exc:
            raise self._wrap_error(exc, action="list_all") from exc
        return [OutcomeRow.from_dict(_payload_dict(row[0])) for row in rows]


__all__ = ["PostgresOutcomeStorage"]
