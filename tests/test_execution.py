from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator
import unittest

from yodb.catalog import (
    Catalog,
    CatalogMetadata,
    DatasetResolution,
    DatasetSpec,
    FieldSpec,
    LogicalType,
    SourceDatasetSpec,
    SourceFieldSpec,
    SourceKind,
    SourceSpec,
)
from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.errors import ErrorCode, QueryExecutionError
from yodb.execution import (
    PostgresQueryExecutionAdapter,
    QueryExecutionAdapterRegistry,
    QueryExecutionEngine,
)
from yodb.inspection import SourceInspection, SourceValidationReport
from yodb.runtime import CatalogEvaluation, SourceRuntimeState, SourceRuntimeStatus


_NOW = datetime(2026, 9, 17, tzinfo=UTC)


class QueryExecutionEngineTests(unittest.TestCase):
    def test_runs_the_full_single_source_postgres_path(self) -> None:
        connection = FakeConnection(rows=[("customer-1", "Acme Corporation", "active")])
        connections = FakePostgresConnections(connection)
        engine = QueryExecutionEngine(
            StaticRuntime(_active_catalog()),
            QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([PostgresQueryExecutionAdapter(connections)]),
        )

        result = engine.execute(
            {
                "from": {"dataset": "customer"},
                "select": ["name", "status"],
                "where": {
                    "any": [
                        {"field": "status", "op": "eq", "value": "active"},
                        {"field": "status", "op": "eq", "value": "pending"},
                    ]
                },
                "page": {"first": 10},
            },
            timeout_seconds=2.0,
        )

        self.assertEqual(
            connection.executed,
            [
                (
                    "\n".join(
                        (
                            'SELECT "account_uuid" AS "id", "company_name" AS "name", '
                            '"account_status" AS "status"',
                            'FROM "public"."accounts"',
                            'WHERE ("account_status" = %s OR "account_status" = %s)',
                            'ORDER BY "account_uuid" ASC',
                            'LIMIT %s',
                        )
                    ),
                    ("active", "pending", 10),
                )
            ],
        )
        self.assertEqual(connections.timeouts, [2.0])
        self.assertEqual(
            [dict(row) for row in result.rows],
            [{"name": "Acme Corporation", "status": "active", "id": "customer-1"}],
        )
        self.assertTrue(result.query_fingerprint)
        self.assertTrue(result.catalog_fingerprint)

    def test_execution_registry_returns_a_structured_error_for_an_unregistered_backend(self) -> None:
        registry = QueryExecutionAdapterRegistry([])

        with self.assertRaises(QueryExecutionError) as caught:
            registry.adapter_for(SourceKind.NEO4J)

        self.assertEqual(caught.exception.code, ErrorCode.QUERY_EXECUTION_UNSUPPORTED)


class StaticRuntime:
    def __init__(self, active: CatalogEvaluation) -> None:
        self._active = active

    def require_active(self) -> CatalogEvaluation:
        return self._active


class FakePostgresConnections:
    source_kind = SourceKind.POSTGRES

    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection
        self.timeouts: list[float | None] = []

    @contextmanager
    def acquire(
        self,
        connection_ref: str,
        *,
        timeout_seconds: float | None = None,
    ) -> Iterator["FakeConnection"]:
        self.timeouts.append(timeout_seconds)
        self._connection.connection_refs.append(connection_ref)
        yield self._connection


class FakeConnection:
    def __init__(self, *, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.connection_refs: list[str] = []

    @contextmanager
    def cursor(self) -> Iterator["FakeCursor"]:
        yield FakeCursor(self)


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    def execute(self, sql: str, parameters: tuple[object, ...]) -> None:
        self._connection.executed.append((sql, parameters))

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._connection._rows


def _active_catalog() -> CatalogEvaluation:
    source = SourceSpec(
        kind=SourceKind.POSTGRES,
        connection_ref="secret://crm-readonly",
        read_only=True,
        datasets={
            "customer": SourceDatasetSpec(
                resource="public.accounts",
                identity=("id",),
                fields={
                    "id": SourceFieldSpec(physical_name="account_uuid"),
                    "name": SourceFieldSpec(physical_name="company_name"),
                    "status": SourceFieldSpec(physical_name="account_status"),
                },
            )
        },
    )
    catalog = Catalog(
        metadata=CatalogMetadata(name="execution", version=1),
        datasets={
            "customer": DatasetSpec(
                description="A customer.",
                fields={
                    "id": FieldSpec(type=LogicalType.ID, description="Stable identity."),
                    "name": FieldSpec(type=LogicalType.STRING, description="Customer name."),
                    "status": FieldSpec(type=LogicalType.STRING, description="Lifecycle status."),
                },
            )
        },
        sources={"crm_postgres": source},
        resolution={
            "customer": DatasetResolution(
                identity_source="crm_postgres",
                field_sources={"id": "crm_postgres", "name": "crm_postgres", "status": "crm_postgres"},
            )
        },
        relationships={},
    )
    return CatalogEvaluation(
        catalog=catalog,
        evaluated_at=_NOW,
        sources={
            "crm_postgres": SourceRuntimeState(
                source_name="crm_postgres",
                status=SourceRuntimeStatus.VALID,
                inspection=SourceInspection(
                    source_name="crm_postgres",
                    source_kind=SourceKind.POSTGRES,
                    inspected_at=_NOW,
                ),
                validation=SourceValidationReport(source_name="crm_postgres", inspected_at=_NOW),
            )
        },
    )
