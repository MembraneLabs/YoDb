"""PostgreSQL statistics from the server's own catalog (``pg_class``/``pg_stats``).

Reads only what ``ANALYZE`` already collected, so it costs two tiny catalog
queries per table and is cached.  Anything missing (never analyzed, no
privilege, driver error) is simply unknown.
"""

from __future__ import annotations

from collections.abc import Callable
import time
from threading import Lock
from typing import Any

from ..connections import SourceConnectionAdapter
from ..query.resolution import SingleSourceQueryBinding
from .statistics import ColumnStatistics, SourceStatistics

_ROWS_SQL = "/* yodb:stats_rows */ SELECT reltuples::double precision FROM pg_class WHERE oid = to_regclass(%s)"
_COLUMNS_SQL = """/* yodb:stats_columns */
SELECT attname, n_distinct, null_frac
FROM pg_stats
WHERE tablename = %s
  AND (schemaname = %s OR (%s::text IS NULL AND schemaname = ANY (current_schemas(false))))
  AND attname = ANY (%s)
"""


class PostgresStatisticsProvider:
    """Row counts and per-column distinct/null statistics from ``pg_stats``.

    Min/max bounds are not read (``pg_stats`` stores them as untyped histogram
    text), so range estimates use the flat default.  Results are cached per
    table for ``ttl_seconds``; a failed or empty lookup is cached too, so a
    source without statistics is not re-queried on every plan.
    """

    def __init__(
        self,
        connections: SourceConnectionAdapter[Any],
        *,
        ttl_seconds: float = 300.0,
        timeout_seconds: float = 2.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0 or timeout_seconds <= 0:
            raise ValueError("ttl_seconds must not be negative and timeout_seconds must be positive")
        self._connections = connections
        self._ttl = ttl_seconds
        self._timeout = timeout_seconds
        self._clock = clock
        self._cache: dict[tuple[str, str], tuple[float, SourceStatistics | None]] = {}
        self._lock = Lock()
        # Class name of the most recent failure, so "unknown" can be diagnosed
        # without exposing driver messages (which may contain connection details).
        self.last_error: str | None = None

    def statistics(self, source: SingleSourceQueryBinding) -> SourceStatistics | None:
        key = (source.connection_ref, source.resource)
        now = self._clock()
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None and cached[0] > now:
                return cached[1]
        try:
            result = self._read(source)
        except Exception as error:  # noqa: BLE001 - unknown, never a query failure
            result = None
            self.last_error = type(error).__name__
        with self._lock:
            self._cache[key] = (now + self._ttl, result)
        return result

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def _read(self, source: SingleSourceQueryBinding) -> SourceStatistics | None:
        parts = source.resource.split(".")
        if not all(parts) or len(parts) > 2:
            return None
        schema, table = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
        qualified = ".".join('"' + part.replace('"', '""') + '"' for part in parts)
        physical = {field.physical_name: field.field.name for field in (source.logical_id, *source.fields)}
        with self._connections.acquire(source.connection_ref, timeout_seconds=self._timeout) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_ROWS_SQL, (qualified,))
                rows = cursor.fetchall()
                reltuples = rows[0][0] if rows else None
                # -1 (PostgreSQL 14+) or NULL: never analyzed or no such table.
                if reltuples is None or reltuples < 0:
                    return None
                cursor.execute(_COLUMNS_SQL, (table, schema, schema, list(physical)))
                column_rows = cursor.fetchall()
        columns: dict[str, ColumnStatistics] = {}
        for name, n_distinct, null_fraction in column_rows:
            logical = physical.get(name)
            if logical is None:
                continue
            # n_distinct > 0 is a count; < 0 is a fraction of the row count; 0 is unknown.
            distinct = None
            if n_distinct is not None and n_distinct > 0:
                distinct = float(n_distinct)
            elif n_distinct is not None and n_distinct < 0:
                distinct = max(1.0, -float(n_distinct) * reltuples)
            columns[logical] = ColumnStatistics(
                distinct_count=distinct,
                null_fraction=None if null_fraction is None else min(1.0, max(0.0, float(null_fraction))),
            )
        return SourceStatistics(row_count=float(reltuples), columns=columns)
