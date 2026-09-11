"""Postgres-backed ledger storage with INSERT ... ON CONFLICT claim semantics."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any, TypeVar

from mycelium.storage._helpers import (
    ClaimOutcome,
    claim_inflight_outcome,
    redact_secrets,
    with_lease,
)
from mycelium.storage.transition_query import TransitionPage, decode_cursor, encode_cursor

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
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        retention_seconds: float | None = None,
        connect_timeout: float = 5.0,
        pool_timeout: float = 5.0,
    ) -> None:
        psycopg, sql = _require_psycopg()
        self._psycopg = psycopg
        self._sql = sql
        self._dsn = dsn
        self._table = _validate_table_name(table)
        self._from_dict = from_dict
        if pool_min_size < 0 or pool_max_size < 1 or pool_min_size > pool_max_size:
            raise ValueError("Postgres pool sizes must satisfy 0 <= min_size <= max_size")
        self._pool_min_size = pool_min_size
        self._pool_max_size = pool_max_size
        self.retention_seconds = retention_seconds
        if connect_timeout <= 0 or pool_timeout <= 0:
            raise ValueError("Postgres timeouts must be positive")
        self._connect_timeout = float(connect_timeout)
        self._pool_timeout = float(pool_timeout)
        self._pool: Any | None = None
        self._schema_ready = False

    def _connection(self) -> Any:
        if self._pool is None:
            try:
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise ImportError(
                    "Postgres pooling requires 'psycopg_pool'. Install with: "
                    "pip install 'mycelium-runtime[postgres]'"
                ) from exc
            self._pool = ConnectionPool(
                self._dsn,
                min_size=self._pool_min_size,
                max_size=self._pool_max_size,
                timeout=self._pool_timeout,
                kwargs={"connect_timeout": self._connect_timeout},
                open=True,
            )
        return self._pool.connection()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()

    def validate(self) -> None:
        try:
            self._ensure_schema()
            with self._connection() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception as exc:
            detail = redact_secrets(str(exc))
            raise RuntimeError(
                "Postgres ledger storage is not ready: "
                f"{type(exc).__name__}: {detail}"
            ) from None

    def _table_id(self) -> Any:
        return self._sql.Identifier(self._table)

    def _effect_index_id(self) -> Any:
        return self._sql.Identifier(f"{self._table}_effect_id_unique")

    def _index_id(self, suffix: str) -> Any:
        return self._sql.Identifier(f"{self._table}_{suffix}")

    @staticmethod
    def _effect_id_for_entry(entry: Any) -> str:
        return str(getattr(entry, "effect_id", None) or entry.request_id)

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        query = self._sql.SQL(
            "CREATE TABLE IF NOT EXISTS {} (request_id TEXT PRIMARY KEY, payload JSONB NOT NULL)"
        ).format(self._table_id())
        effect_index = self._sql.SQL(
            "CREATE UNIQUE INDEX IF NOT EXISTS {} ON {} "
            "((COALESCE(payload->>'effect_id', request_id)))"
        ).format(self._effect_index_id(), self._table_id())
        outcome_time_index = self._sql.SQL(
            "CREATE INDEX IF NOT EXISTS {} ON {} "
            "((COALESCE(payload->>'terminal_outcome', "
            "CASE payload->>'status' WHEN 'completed' THEN 'COMPLETED' "
            "WHEN 'failed' THEN 'FAILED_BEFORE_EFFECT' ELSE 'IN_FLIGHT' END)), "
            "((payload->>'started_at')::double precision), request_id)"
        ).format(self._index_id("outcome_started_idx"), self._table_id())
        finished_index = self._sql.SQL(
            "CREATE INDEX IF NOT EXISTS {} ON {} "
            "(((payload->>'finished_at')::double precision), request_id) "
            "WHERE payload->>'finished_at' IS NOT NULL"
        ).format(self._index_id("finished_idx"), self._table_id())
        with self._connection() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"mycelium:schema:ledger:{self._table}",),
            )
            conn.execute(query)
            conn.execute(effect_index)
            conn.execute(outcome_time_index)
            conn.execute(finished_index)
        self._schema_ready = True

    def get(self, request_id: str) -> E | None:
        self._ensure_schema()
        query = self._sql.SQL("SELECT payload FROM {} WHERE request_id = %s").format(
            self._table_id()
        )
        with self._connection() as conn:
            row = conn.execute(query, (request_id,)).fetchone()
        if row is None:
            return None
        return self._from_dict(_payload_dict(row[0]))

    def set(self, entry: E) -> None:
        self._ensure_schema()
        payload = json.loads(json.dumps(entry.to_dict(), default=str))
        effect_id = self._effect_id_for_entry(entry)
        lookup_query = self._sql.SQL(
            "SELECT request_id FROM {} "
            "WHERE COALESCE(payload->>'effect_id', request_id) = %s "
            "LIMIT 1"
        ).format(self._table_id())
        query = self._sql.SQL(
            "INSERT INTO {} (request_id, payload) VALUES (%s, %s::jsonb) "
            "ON CONFLICT (request_id) DO UPDATE SET payload = EXCLUDED.payload"
        ).format(self._table_id())
        with self._connection() as conn:
            existing = conn.execute(lookup_query, (effect_id,)).fetchone()
            if existing is not None and str(existing[0]) != entry.request_id:
                conn.commit()
                return
            conn.execute(query, (entry.request_id, json.dumps(payload)))

    def try_claim_inflight(
        self,
        entry: E,
        *,
        lease_ttl: float = 3600.0,
    ) -> tuple[ClaimOutcome, E | None]:
        self._ensure_schema()
        now = time.time()
        effect_id = self._effect_id_for_entry(entry)
        fresh = with_lease(entry, now=now, lease_ttl=lease_ttl)
        payload = json.loads(json.dumps(fresh.to_dict(), default=str))
        insert_query = self._sql.SQL(
            "INSERT INTO {} (request_id, payload) VALUES (%s, %s::jsonb) "
            "ON CONFLICT DO NOTHING RETURNING request_id"
        ).format(self._table_id())
        select_for_update = self._sql.SQL(
            "SELECT request_id, payload FROM {} WHERE request_id = %s FOR UPDATE"
        ).format(self._table_id())
        select_by_effect = self._sql.SQL(
            "SELECT request_id, payload FROM {} "
            "WHERE COALESCE(payload->>'effect_id', request_id) = %s "
            "ORDER BY request_id "
            "LIMIT 1 "
            "FOR UPDATE"
        ).format(self._table_id())
        update_reclaim = self._sql.SQL(
            "UPDATE {} SET payload = %s::jsonb WHERE request_id = %s RETURNING request_id"
        ).format(self._table_id())

        with self._connection() as conn:
            with conn.transaction():
                inserted = conn.execute(
                    insert_query,
                    (entry.request_id, json.dumps(payload)),
                ).fetchone()
                if inserted is not None:
                    return "claimed", None

                row = conn.execute(select_for_update, (entry.request_id,)).fetchone()
                if row is None:
                    row = conn.execute(select_by_effect, (effect_id,)).fetchone()
                if row is None:
                    inserted = conn.execute(
                        insert_query,
                        (entry.request_id, json.dumps(payload)),
                    ).fetchone()
                    if inserted is not None:
                        return "claimed", None
                    row = conn.execute(select_by_effect, (effect_id,)).fetchone()
                    if row is None:
                        return "in_flight", None

                active_request_id = str(row[0])
                existing = self._from_dict(_payload_dict(row[1]))
                outcome = claim_inflight_outcome(existing, now=now)
                if outcome == "completed":
                    return "completed", existing
                if outcome == "in_flight":
                    return "in_flight", existing

                claim_entry = (
                    entry
                    if active_request_id == entry.request_id
                    else replace(entry, request_id=active_request_id)
                )
                reclaim_entry = with_lease(
                    claim_entry,
                    now=now,
                    lease_ttl=lease_ttl,
                    prior=existing,
                )
                reclaim_payload = json.loads(json.dumps(reclaim_entry.to_dict(), default=str))
                reclaimed = conn.execute(
                    update_reclaim,
                    (json.dumps(reclaim_payload), active_request_id),
                ).fetchone()
                if reclaimed is not None:
                    return "claimed", None
                return "in_flight", existing

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
        self._ensure_schema()
        table = self._table_id()
        payload = json.loads(json.dumps(entry.to_dict(), default=str))
        # Build WHERE clause: terminal_outcome IN (...) [AND owner = ...]
        # [AND fence matches] [AND lease held or unbounded]
        extra_clauses = self._sql.SQL("")
        params: list[Any] = [
            json.dumps(payload),
            entry.request_id,
            list(expected_terminal_outcomes),
        ]
        if expected_owner is not None:
            extra_clauses = self._sql.SQL("{} AND payload->>'owner' = %s").format(extra_clauses)
            params.append(expected_owner)
        if expected_fence is not None:
            # COALESCE so old rows (payload without a fence) read as 0.
            extra_clauses = self._sql.SQL(
                "{} AND COALESCE((payload->>'fence')::bigint, 0) = %s"
            ).format(extra_clauses)
            params.append(expected_fence)
        if expected_effect_state is not None:
            extra_clauses = self._sql.SQL(
                "{} AND COALESCE(payload->>'effect_phase', 'INTENDED') = %s"
            ).format(extra_clauses)
            params.append(expected_effect_state)
        if require_lease_held_at is not None:
            # NULL lease_until = unbounded; else must still be in the future.
            extra_clauses = self._sql.SQL(
                "{} AND (payload->>'lease_until' IS NULL "
                "OR (payload->>'lease_until')::double precision > %s)"
            ).format(extra_clauses)
            params.append(require_lease_held_at)
        query = self._sql.SQL(
            "UPDATE {} SET payload = %s::jsonb "
            "WHERE request_id = %s "
            "AND payload->>'terminal_outcome' = ANY(%s) {} "
            "RETURNING request_id"
        ).format(table, extra_clauses)
        with self._connection() as conn:
            row = conn.execute(query, tuple(params)).fetchone()
        return row is not None

    def list_all(self) -> list[E]:
        self._ensure_schema()
        query = self._sql.SQL("SELECT payload FROM {}").format(self._table_id())
        with self._connection() as conn:
            rows = conn.execute(query).fetchall()
        return [self._from_dict(_payload_dict(row[0])) for row in rows]

    def list_page(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        tool: str | None = None,
        outcome: str | None = None,
        parent_request_id: str | None = None,
        started_after: float | None = None,
        started_before: float | None = None,
        finished_before: float | None = None,
    ) -> TransitionPage[E]:
        self._ensure_schema()
        if limit < 1 or limit > 10_000:
            raise ValueError("transition page limit must be between 1 and 10000")
        clauses: list[Any] = []
        params: list[Any] = []
        if tool is not None:
            clauses.append(self._sql.SQL("payload->>'tool' = %s"))
            params.append(tool)
        if outcome is not None:
            clauses.append(
                self._sql.SQL(
                    "COALESCE(payload->>'terminal_outcome', "
                    "CASE payload->>'status' WHEN 'completed' THEN 'COMPLETED' "
                    "WHEN 'failed' THEN 'FAILED_BEFORE_EFFECT' ELSE 'IN_FLIGHT' END) = %s"
                )
            )
            params.append(outcome)
        if parent_request_id is not None:
            clauses.append(self._sql.SQL("payload->>'parent_request_id' = %s"))
            params.append(parent_request_id)
        if started_after is not None:
            clauses.append(self._sql.SQL("(payload->>'started_at')::double precision >= %s"))
            params.append(started_after)
        if started_before is not None:
            clauses.append(self._sql.SQL("(payload->>'started_at')::double precision < %s"))
            params.append(started_before)
        order_expression = self._sql.SQL("(payload->>'started_at')::double precision")
        if finished_before is not None:
            clauses.append(self._sql.SQL("payload->>'finished_at' IS NOT NULL"))
            clauses.append(self._sql.SQL("(payload->>'finished_at')::double precision < %s"))
            params.append(finished_before)
            order_expression = self._sql.SQL("(payload->>'finished_at')::double precision")
        decoded = decode_cursor(cursor)
        if decoded is not None:
            clauses.append(
                self._sql.SQL(
                    "({}, request_id) > (%s, %s)"
                ).format(order_expression)
            )
            params.extend(decoded)
        where = (
            self._sql.SQL(" WHERE ") + self._sql.SQL(" AND ").join(clauses)
            if clauses
            else self._sql.SQL("")
        )
        query = self._sql.SQL(
            "SELECT payload FROM {}{} ORDER BY {}, request_id LIMIT %s"
        ).format(self._table_id(), where, order_expression)
        params.append(limit + 1)
        with self._connection() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()
        entries = [self._from_dict(_payload_dict(row[0])) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit and entries:
            last = entries[-1]
            cursor_time = last.finished_at if finished_before is not None else last.started_at
            next_cursor = encode_cursor(float(cursor_time or 0.0), last.request_id)
        return TransitionPage(entries, next_cursor)

    def delete_entries(self, request_ids: list[str]) -> int:
        self._ensure_schema()
        if not request_ids:
            return 0
        query = self._sql.SQL("DELETE FROM {} WHERE request_id = ANY(%s)").format(
            self._table_id()
        )
        with self._connection() as conn:
            result = conn.execute(query, (request_ids,))
        return int(result.rowcount)

    def resolve_request_id(self, effect_id: str) -> str | None:
        self._ensure_schema()
        query = self._sql.SQL(
            "SELECT request_id FROM {} "
            "WHERE COALESCE(payload->>'effect_id', request_id) = %s "
            "ORDER BY request_id LIMIT 1"
        ).format(self._table_id())
        with self._connection() as conn:
            row = conn.execute(query, (effect_id,)).fetchone()
        if row is None:
            return None
        return str(row[0])

    def get_by_effect_id(self, effect_id: str) -> E | None:
        request_id = self.resolve_request_id(effect_id)
        if request_id is None:
            return None
        return self.get(request_id)


class PostgresLedgerStorage:
    """Postgres storage for :class:`~mycelium.action_ledger.LedgerEntry`."""

    def __init__(
        self,
        dsn: str,
        *,
        table: str = "mycelium_action_ledger",
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        retention_seconds: float | None = None,
        connect_timeout: float = 5.0,
        pool_timeout: float = 5.0,
    ) -> None:
        from mycelium.ledger_model import LedgerEntry

        self._inner = PostgresEntryStorage(
            dsn,
            table=table,
            from_dict=LedgerEntry.from_dict,
            pool_min_size=pool_min_size,
            pool_max_size=pool_max_size,
            retention_seconds=retention_seconds,
            connect_timeout=connect_timeout,
            pool_timeout=pool_timeout,
        )
        self.retention_seconds = retention_seconds

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

    def list_page(self, **kwargs: Any) -> TransitionPage[Any]:
        return self._inner.list_page(**kwargs)

    def delete_entries(self, request_ids: list[str]) -> int:
        return self._inner.delete_entries(request_ids)

    def close(self) -> None:
        self._inner.close()

    def validate(self) -> None:
        self._inner.validate()

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
