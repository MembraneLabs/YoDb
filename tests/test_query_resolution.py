from __future__ import annotations

from datetime import UTC, datetime
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
from yodb.errors import ErrorCode, QueryError
from yodb.inspection import SourceInspection, SourceValidationReport
from yodb.query import FieldUse, QuerySourceShape, bind_query, parse_query, resolve_query_sources
from yodb.runtime import CatalogEvaluation, SourceRuntimeState, SourceRuntimeStatus


_NOW = datetime(2026, 9, 17, tzinfo=UTC)


class QuerySourceResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.active = _active_catalog()

    def test_classifies_a_query_with_one_source_and_resolves_physical_fields(self) -> None:
        query = bind_query(
            parse_query(
                {
                    "from": {"dataset": "customer"},
                    "select": ["name", "status"],
                    "where": {"field": "status", "op": "eq", "value": "active"},
                    "order_by": [{"field": "name", "direction": "asc"}],
                }
            ),
            self.active,
        )

        resolved = resolve_query_sources(query, self.active)

        self.assertEqual(resolved.shape, QuerySourceShape.SINGLE_SOURCE)
        self.assertEqual(resolved.identity_source.source_name, "crm_postgres")
        self.assertEqual(resolved.identity_source.resource, "public.accounts")
        self.assertEqual(resolved.identity_source.logical_id.physical_name, "account_uuid")
        fields = {field.field.name: field for field in resolved.identity_source.fields}
        self.assertEqual(fields["name"].physical_name, "company_name")
        self.assertEqual(fields["status"].physical_name, "account_status")
        self.assertEqual(fields["name"].uses, frozenset({FieldUse.PROJECTION, FieldUse.ORDER}))
        self.assertEqual(fields["status"].uses, frozenset({FieldUse.PROJECTION, FieldUse.FILTER}))
        self.assertEqual(resolved.logical_id_links, ())

    def test_composes_multi_source_bindings_with_explicit_logical_id_link(self) -> None:
        query = bind_query(
            parse_query(
                {
                    "from": {"dataset": "customer"},
                    "select": ["name", "plan"],
                    "where": {"field": "plan", "op": "eq", "value": "enterprise"},
                    "order_by": [{"field": "name", "direction": "asc"}],
                }
            ),
            self.active,
        )

        resolved = resolve_query_sources(query, self.active)

        self.assertEqual(resolved.shape, QuerySourceShape.MULTI_SOURCE)
        self.assertEqual([source.source_name for source in resolved.sources], ["crm_postgres", "billing_postgres"])
        crm, billing = resolved.sources
        self.assertEqual([field.field.name for field in crm.projection_fields], ["id", "name"])
        self.assertEqual([field.field.name for field in billing.projection_fields], ["plan"])
        self.assertEqual([field.field.name for field in billing.filter_fields], ["plan"])
        self.assertEqual(billing.logical_id.physical_name, "crm_customer_uuid")
        self.assertEqual(len(resolved.logical_id_links), 1)
        link = resolved.logical_id_links[0]
        self.assertEqual(link.from_source, "crm_postgres")
        self.assertEqual(link.from_logical_id.physical_name, "account_uuid")
        self.assertEqual(link.to_source, "billing_postgres")
        self.assertEqual(link.to_logical_id.physical_name, "crm_customer_uuid")

    def test_rejects_contributor_without_a_unique_logical_id_mapping(self) -> None:
        broken = _active_catalog(billing_identity=("plan",))
        query = bind_query(
            parse_query({"from": {"dataset": "customer"}, "select": ["plan"]}),
            broken,
        )

        with self.assertRaises(QueryError) as caught:
            resolve_query_sources(query, broken)

        self.assertEqual(caught.exception.code, ErrorCode.SOURCE_LOGICAL_ID_UNAVAILABLE)
        self.assertEqual(
            caught.exception.detail.location,
            "sources.billing_postgres.datasets.customer.identity",
        )

    def test_rejects_a_bound_query_against_a_different_catalog_snapshot(self) -> None:
        query = bind_query(parse_query({"from": {"dataset": "customer"}, "select": ["name"]}), self.active)
        changed = _active_catalog(version=2)

        with self.assertRaises(QueryError) as caught:
            resolve_query_sources(query, changed)

        self.assertEqual(caught.exception.code, ErrorCode.QUERY_CATALOG_MISMATCH)


def _active_catalog(
    *,
    version: int = 1,
    billing_identity: tuple[str, ...] = ("id",),
) -> CatalogEvaluation:
    catalog = Catalog(
        metadata=CatalogMetadata(name="source_resolution", version=version),
        datasets={
            "customer": DatasetSpec(
                description="A commercial customer.",
                fields={
                    "id": FieldSpec(type=LogicalType.ID, description="Stable logical identity."),
                    "name": FieldSpec(type=LogicalType.STRING, description="Customer name."),
                    "status": FieldSpec(type=LogicalType.STRING, description="Lifecycle status."),
                    "plan": FieldSpec(type=LogicalType.STRING, description="Billing plan."),
                },
            )
        },
        sources={
            "crm_postgres": SourceSpec(
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
            ),
            "billing_postgres": SourceSpec(
                kind=SourceKind.POSTGRES,
                connection_ref="secret://billing-readonly",
                read_only=True,
                datasets={
                    "customer": SourceDatasetSpec(
                        resource="billing.customers",
                        identity=billing_identity,
                        fields={
                            "id": SourceFieldSpec(physical_name="crm_customer_uuid"),
                            "plan": SourceFieldSpec(physical_name="subscription_plan"),
                        },
                    )
                },
            ),
        },
        resolution={
            "customer": DatasetResolution(
                identity_source="crm_postgres",
                field_sources={
                    "id": "crm_postgres",
                    "name": "crm_postgres",
                    "status": "crm_postgres",
                    "plan": "billing_postgres",
                },
            )
        },
        relationships={},
    )
    states = {
        source_name: SourceRuntimeState(
            source_name=source_name,
            status=SourceRuntimeStatus.VALID,
            inspection=SourceInspection(
                source_name=source_name,
                source_kind=source.kind,
                inspected_at=_NOW,
            ),
            validation=SourceValidationReport(source_name=source_name, inspected_at=_NOW),
        )
        for source_name, source in catalog.sources.items()
    }
    return CatalogEvaluation(catalog=catalog, evaluated_at=_NOW, sources=states)
