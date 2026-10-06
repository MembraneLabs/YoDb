"""A small in-memory 'database world' for tests: real filtering, key lists and limits, no server.

The compiler's SQL is run on SQLite (``%s`` becomes ``?``; each schema is an attached database),
so a source returns exactly the rows its query asks for.  Use it where a test must see what a
predicate or an ``IN`` list really selects; use the plain fakes where it does not matter.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import weakref

from yodb.catalog import SourceKind, load_catalog
from yodb.inspection import SourceInspection, SourceValidationReport
from yodb.runtime import CatalogEvaluation, SourceRuntimeState, SourceRuntimeStatus

_NOW = datetime(2026, 10, 3, tzinfo=UTC)


def evaluation(datasets: str, sources: str, relations: str) -> CatalogEvaluation:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for name, text in (("datasets", datasets), ("sources", sources), ("relations", relations)):
            (root / f"{name}.yaml").write_text(text, encoding="utf-8")
        catalog = load_catalog(root)
    return CatalogEvaluation(
        catalog=catalog,
        evaluated_at=_NOW,
        sources={
            name: SourceRuntimeState(
                source_name=name,
                status=SourceRuntimeStatus.VALID,
                inspection=SourceInspection(source_name=name, source_kind=source.kind, inspected_at=_NOW),
                validation=SourceValidationReport(source_name=name, inspected_at=_NOW),
            )
            for name, source in catalog.sources.items()
        },
    )


class SqlWorld:
    """Executes compiled PostgreSQL-dialect scans against in-memory tables."""

    source_kind = SourceKind.POSTGRES

    def __init__(self, tables: dict[str, tuple[tuple[str, ...], list[tuple]]]) -> None:
        """``tables``: ``"schema.table" -> (column names, rows)``."""

        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        weakref.finalize(self, self._db.close)          # closed when the world is garbage, so tests need no cleanup
        attached = set()
        for resource, (columns, rows) in tables.items():
            schema, table = resource.split(".")
            if schema not in attached:
                self._db.execute(f'ATTACH DATABASE ":memory:" AS "{schema}"')
                attached.add(schema)
            self._db.execute(f'CREATE TABLE "{schema}"."{table}" ({", ".join(f"{c}" for c in columns)})')
            self._db.executemany(f'INSERT INTO "{schema}"."{table}" VALUES ({", ".join("?" for _ in columns)})', rows)
        self.queries = []

    def execute(self, query, *, timeout_seconds=None):
        self.queries.append(query)
        cursor = self._db.execute(query.sql.replace("%s", "?"), query.parameters)
        names = [column.logical_field for column in query.output_columns]
        return tuple(dict(zip(names, row)) for row in cursor.fetchall())

    def reads_of(self, source: str) -> list:
        return [q for q in self.queries if q.source_name == source]
