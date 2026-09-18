from __future__ import annotations

import unittest

from yodb.catalog import DatasetSpec, FieldSpec, LogicalType, SourceKind
from yodb.cost import CostConfidence, PostgresCostEstimator
from yodb.inspection import PhysicalField, PhysicalIndex, PhysicalResource, ResourceKind, SourceInspection
from yodb.planning import SourceScanPlan
from yodb.query import BoundDataset, BoundField, BoundPredicate, ComparisonOperator, FieldUse, ResolvedField, SingleSourceQueryBinding


class PostgresCostEstimatorTests(unittest.TestCase):
    def test_uses_row_count_distinct_values_and_index_for_an_equality_scan(self) -> None:
        source, identifier, status = _source()
        scan = SourceScanPlan(
            source=source,
            projection=(identifier,),
            where=BoundPredicate(status.field, ComparisonOperator.EQ, "active", True),
            order_by=(),
            limit=None,
        )
        inspection = SourceInspection(
            source_name="crm_postgres",
            source_kind=SourceKind.POSTGRES,
            inspected_at="2026-09-17T00:00:00Z",
            resources={
                "public.accounts": PhysicalResource(
                    name="public.accounts",
                    kind=ResourceKind.TABLE,
                    estimated_rows=1_000_000,
                    average_row_bytes=120,
                    fields={
                        "account_uuid": PhysicalField(name="account_uuid", native_type="uuid", type_family="uuid", estimated_distinct_values=1_000_000, average_value_bytes=16),
                        "account_status": PhysicalField(name="account_status", native_type="text", type_family="string", estimated_distinct_values=5, average_value_bytes=8),
                    },
                    indexes=(PhysicalIndex(name="accounts_status", fields=("account_status",), method="btree"),),
                )
            },
        )

        estimate = PostgresCostEstimator().estimate_scan(scan, inspection)

        self.assertEqual(estimate.estimated_rows, 200_000)
        self.assertEqual(estimate.estimated_transfer_bytes, 8_000_000)
        self.assertEqual(estimate.confidence, CostConfidence.MEDIUM)
        self.assertLess(estimate.estimated_backend_work, 1_000_000)


def _source():
    dataset = DatasetSpec(
        description="Customer.",
        fields={
            "id": FieldSpec(type=LogicalType.ID, description="Identity."),
            "status": FieldSpec(type=LogicalType.STRING, description="Status."),
        },
    )
    root = BoundDataset(name="customer", scope="customer", spec=dataset)
    identifier = ResolvedField(
        field=BoundField("customer", "customer", "id", dataset.fields["id"]), source_name="crm_postgres", source_kind=SourceKind.POSTGRES, connection_ref="secret://crm", resource="public.accounts", physical_name="account_uuid", uses=frozenset({FieldUse.IDENTITY, FieldUse.PROJECTION}),
    )
    status = ResolvedField(
        field=BoundField("customer", "customer", "status", dataset.fields["status"]), source_name="crm_postgres", source_kind=SourceKind.POSTGRES, connection_ref="secret://crm", resource="public.accounts", physical_name="account_status", uses=frozenset({FieldUse.FILTER}),
    )
    return SingleSourceQueryBinding("crm_postgres", SourceKind.POSTGRES, "secret://crm", "public.accounts", identifier, (identifier, status)), identifier, status
