"""PostgreSQL statistics from the server's own catalog (``pg_class``/``pg_stats``).

Reads only what ``ANALYZE`` already collected, so it costs two tiny catalog
queries per table and is cached.  Anything missing (never analyzed, no
privilege, driver error) is simply unknown.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time
from threading import Lock
from typing import Any

from ..catalog import LogicalType
from ..connections import SourceConnectionAdapter
from ..query.resolution import SingleSourceQueryBinding
from .statistics import ColumnStatistics, SourceStatistics

_ROWS_SQL = "/* yodb:stats_rows */ SELECT reltuples::double precision FROM pg_class WHERE oid = to_regclass(%s)"
_COLUMNS_SQL = """/* yodb:stats_columns */
SELECT attname, n_distinct, null_frac, most_common_vals::text::text[], most_common_freqs::float8[]
FROM pg_stats
WHERE tablename = %s
  AND (schemaname = %s OR (%s::text IS NULL AND schemaname = ANY (current_schemas(false))))
"""


@dataclass(frozen=True)
class _RawColumn:
    """One physical column's statistics as the server reports them (values still text)."""

    distinct: float | None
    null_fraction: float | None
    values: tuple[str, ...]
    frequencies: tuple[float, ...]


@dataclass(frozen=True)
class _TableEntry:
    row_count: float
    columns: dict[str, _RawColumn]       # keyed by physical column name, for the whole table


def _typed(text: str, logical_type: LogicalType) -> object | None:
    """A most-common value in the logical field's type, or None when it cannot be compared safely."""

    try:
        if logical_type in (LogicalType.STRING, LogicalType.TEXT, LogicalType.ID, LogicalType.UUID):
            return text
        if logical_type is LogicalType.INT:
            return int(text)
        if logical_type is LogicalType.FLOAT:
            return float(text)
        if logical_type is LogicalType.BOOL:
            return text.lower() in ("t", "true")
    except ValueError:
        return None
    return None                          # timestamps and the like: not compared by value


class PostgresStatisticsProvider:
    """Row counts and per-column distinct/null statistics from ``pg_stats``.

    Min/max bounds are not read (``pg_stats`` stores them as untyped histogram
    text), so range estimates use the flat default.  Most-common values and their
    frequencies are read, so skewed columns estimate well.  The whole table's
    column statistics are cached for ``ttl_seconds`` (never just the columns of the
    query that happened to ask first); a failed or empty lookup is cached too, so a
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
        self._cache: dict[tuple[str, str], tuple[float, _TableEntry | None]] = {}
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
                return self._for(source, cached[1])
        try:
            entry = self._read(source)
        except Exception as error:  # noqa: BLE001 - unknown, never a query failure
            entry = None
            self.last_error = type(error).__name__
        with self._lock:
            self._cache[key] = (now + self._ttl, entry)
        return self._for(source, entry)

    @staticmethod
    def _for(source: SingleSourceQueryBinding, entry: _TableEntry | None) -> SourceStatistics | None:
        """This query's view of the table: its fields, under their logical names."""

        if entry is None:
            return None
        columns: dict[str, ColumnStatistics] = {}
        for field in (source.logical_id, *source.fields):
            raw = entry.columns.get(field.physical_name)
            if raw is None:
                continue
            typed = [(_typed(text, field.field.spec.type), freq) for text, freq in zip(raw.values, raw.frequencies)]
            columns[field.field.name] = ColumnStatistics(
                distinct_count=raw.distinct,
                null_fraction=raw.null_fraction,
                common_values=tuple((value, freq) for value, freq in typed if value is not None),
            )
        return SourceStatistics(row_count=entry.row_count, columns=columns)

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def _read(self, source: SingleSourceQueryBinding) -> _TableEntry | None:
        parts = source.resource.split(".")
        if not all(parts) or len(parts) > 2:
            return None
        schema, table = (parts[0], parts[1]) if len(parts) == 2 else (None, parts[0])
        qualified = ".".join('"' + part.replace('"', '""') + '"' for part in parts)
        with self._connections.acquire(source.connection_ref, timeout_seconds=self._timeout) as connection:
            with connection.cursor() as cursor:
                cursor.execute(_ROWS_SQL, (qualified,))
                rows = cursor.fetchall()
                reltuples = rows[0][0] if rows else None
                # -1 (PostgreSQL 14+) or NULL: never analyzed or no such table.
                if reltuples is None or reltuples < 0:
                    return None
                cursor.execute(_COLUMNS_SQL, (table, schema, schema))
                column_rows = cursor.fetchall()
        columns: dict[str, _RawColumn] = {}
        for name, n_distinct, null_fraction, common_values, common_freqs in column_rows:
            # n_distinct > 0 is a count; < 0 is a fraction of the row count; 0 is unknown.
            distinct = None
            if n_distinct is not None and n_distinct > 0:
                distinct = float(n_distinct)
            elif n_distinct is not None and n_distinct < 0:
                distinct = max(1.0, -float(n_distinct) * reltuples)
            values = tuple(common_values or ())
            frequencies = tuple(float(f) for f in (common_freqs or ()))
            count = min(len(values), len(frequencies))
            columns[name] = _RawColumn(
                distinct=distinct,
                null_fraction=None if null_fraction is None else min(1.0, max(0.0, float(null_fraction))),
                values=values[:count],
                frequencies=frequencies[:count],
            )
        return _TableEntry(float(reltuples), columns)
