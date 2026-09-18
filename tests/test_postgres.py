from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator
import unittest

from yodb import (
    InspectionCapability,
    InspectionRequest,
    PostgresCatalogValidator,
    PostgresSourceInspector,
)
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


class PostgresAdapterTests(unittest.TestCase):
    def test_inspector_reads_physical_metadata_without_reading_business_rows(self) -> None:
        connections = FakeConnectionAdapter()
        inspector = PostgresSourceInspector(connections)

        inspection = inspector.inspect(InspectionRequest(source_name="crm_postgres", source=_source()))

        connection = connections.connection
        accounts = inspection.resources["public.accounts"]
        self.assertTrue(connection.rolled_back)
        self.assertTrue(connections.returned)
        self.assertIn("SET TRANSACTION READ ONLY", connection.executed)
        self.assertEqual(inspection.engine_version, "17.5")
        self.assertEqual(accounts.primary_key.fields, ("account_uuid",))
        self.assertEqual(accounts.foreign_keys[0].target_resource, "public.users")
        self.assertEqual(accounts.foreign_keys[0].on_delete, "restrict")
        self.assertEqual(accounts.fields["embedding"].dimensions, 1536)
        self.assertIn(InspectionCapability.VECTOR_COLUMNS, inspection.capabilities)
        self.assertIn(InspectionCapability.VECTOR_INDEXES, inspection.capabilities)
        self.assertIn(InspectionCapability.TABLE_STATISTICS, inspection.capabilities)
        self.assertEqual(accounts.estimated_rows, 50_000)
        self.assertEqual(accounts.fields["company_name"].estimated_distinct_values, 40_000)

    def test_validator_accepts_existing_unique_identity_and_mapped_fields(self) -> None:
        inspection = PostgresSourceInspector(FakeConnectionAdapter()).inspect(
            InspectionRequest(source_name="crm_postgres", source=_source())
        )

        report = PostgresCatalogValidator().validate(_catalog(), inspection)

        self.assertTrue(report.is_valid)
        self.assertEqual(report.findings, ())

    def test_validator_reports_a_missing_configured_resource(self) -> None:
        inspection = PostgresSourceInspector(FakeConnectionAdapter()).inspect(
            InspectionRequest(source_name="crm_postgres", source=_source())
        )
        source = _source(resource="public.missing_accounts")
        catalog = _catalog(source=source)

        report = PostgresCatalogValidator().validate(catalog, inspection)

        self.assertFalse(report.is_valid)
        self.assertEqual(report.findings[0].code, "resource_not_found")


class FakeConnection:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.rolled_back = False
        self.closed = False

    def cursor(self) -> "FakeCursor":
        return FakeCursor(self)

    def rollback(self) -> None:
        self.rolled_back = True

    def close(self) -> None:
        self.closed = True


class FakeConnectionAdapter:
    def __init__(self) -> None:
        self.connection = FakeConnection()
        self.returned = False

    @contextmanager
    def acquire(self, connection_ref: str, *, timeout_seconds: float | None = None) -> Iterator[FakeConnection]:
        try:
            yield self.connection
        finally:
            self.connection.rollback()
            self.returned = True


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection
        self._rows: list[dict[str, Any]] = []
        self.description = None

    def execute(self, query: str) -> None:
        normalized = " ".join(query.split())
        if normalized == "SET TRANSACTION READ ONLY":
            self._connection.executed.append(normalized)
            return
        marker = query.split("*/", 1)[0].removeprefix("/* yodb:").strip()
        self._connection.executed.append(marker)
        self._rows = _RESPONSES[marker]

    def fetchall(self) -> list[dict[str, Any]]:
        return self._rows

    def close(self) -> None:
        pass


_RESPONSES: dict[str, list[dict[str, Any]]] = {
    "server_version": [{"server_version": "17.5"}],
    "extensions": [{"extname": "vector", "extversion": "0.8.0"}],
    "resources": [
        {"resource_name": "public.accounts", "resource_kind": "table"},
        {"resource_name": "public.users", "resource_kind": "table"},
    ],
    "columns": [
        {
            "resource_name": "public.accounts",
            "field_name": "account_uuid",
            "native_type": "uuid",
            "nullable": False,
            "default_expression": None,
            "generated": False,
        },
        {
            "resource_name": "public.accounts",
            "field_name": "company_name",
            "native_type": "text",
            "nullable": False,
            "default_expression": None,
            "generated": False,
        },
        {
            "resource_name": "public.accounts",
            "field_name": "owner_id",
            "native_type": "uuid",
            "nullable": False,
            "default_expression": None,
            "generated": False,
        },
        {
            "resource_name": "public.accounts",
            "field_name": "embedding",
            "native_type": "vector(1536)",
            "nullable": True,
            "default_expression": None,
            "generated": False,
        },
    ],
    "keys_and_checks": [
        {
            "resource_name": "public.accounts",
            "constraint_name": "accounts_pkey",
            "constraint_type": "p",
            "fields": ["account_uuid"],
            "definition": "PRIMARY KEY (account_uuid)",
        }
    ],
    "foreign_keys": [
        {
            "resource_name": "public.accounts",
            "constraint_name": "accounts_owner_id_fkey",
            "fields": ["owner_id"],
            "target_resource": "public.users",
            "target_fields": ["id"],
            "update_action": "c",
            "delete_action": "r",
            "deferrable": False,
            "initially_deferred": False,
        }
    ],
    "indexes": [
        {
            "resource_name": "public.accounts",
            "index_name": "accounts_pkey",
            "method": "btree",
            "is_unique": True,
            "is_valid": True,
            "fields": ["account_uuid"],
            "include_fields": [],
            "predicate": None,
            "definition": "CREATE UNIQUE INDEX accounts_pkey ON public.accounts USING btree (account_uuid)",
        },
        {
            "resource_name": "public.accounts",
            "index_name": "accounts_embedding_hnsw",
            "method": "hnsw",
            "is_unique": False,
            "is_valid": True,
            "fields": ["embedding"],
            "include_fields": [],
            "predicate": None,
            "definition": "CREATE INDEX accounts_embedding_hnsw ON public.accounts USING hnsw (embedding)",
        },
    ],
    "resource_statistics": [
        {"resource_name": "public.accounts", "estimated_rows": 50_000, "average_row_bytes": 180},
        {"resource_name": "public.users", "estimated_rows": 20_000, "average_row_bytes": 120},
    ],
    "column_statistics": [
        {
            "resource_name": "public.accounts",
            "field_name": "company_name",
            "null_frac": 0.0,
            "n_distinct": 40_000,
            "avg_width": 34,
        },
        {
            "resource_name": "public.accounts",
            "field_name": "account_uuid",
            "null_frac": 0.0,
            "n_distinct": -1.0,
            "avg_width": 16,
        },
    ],
}


def _source(*, resource: str = "public.accounts") -> SourceSpec:
    return SourceSpec(
        kind=SourceKind.POSTGRES,
        connection_ref="secret://yodb/crm-readonly",
        read_only=True,
        datasets={
            "customer": SourceDatasetSpec(
                resource=resource,
                identity=("id",),
                fields={
                    "id": SourceFieldSpec(physical_name="account_uuid"),
                    "name": SourceFieldSpec(physical_name="company_name"),
                },
            )
        },
    )


def _catalog(*, source: SourceSpec | None = None) -> Catalog:
    source = source or _source()
    return Catalog(
        metadata=CatalogMetadata(name="acme_data", version=1),
        datasets={
            "customer": DatasetSpec(
                description="A company with an account.",
                fields={
                    "id": FieldSpec(type=LogicalType.ID, description="Stable customer identity."),
                    "name": FieldSpec(type=LogicalType.STRING, description="Company name."),
                },
            )
        },
        sources={"crm_postgres": source},
        resolution={
            "customer": DatasetResolution(
                identity_source="crm_postgres", field_sources={"id": "crm_postgres", "name": "crm_postgres"}
            )
        },
        relationships={},
    )
