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
from yodb.cost import (
    CostEstimatorRegistry,
    FederatedCostPolicy,
    FederatedPlanEstimator,
    FederatedQueryBudget,
    PostgresCostEstimator,
)
from yodb.inspection import PhysicalField, PhysicalIndex, PhysicalKey, PhysicalResource, ResourceKind, SourceInspection, SourceValidationReport
from yodb.planning import (
    FederatedPhysicalPlanner,
    PostgresSourceCapabilities,
    SourceCapabilityRegistry,
)
from yodb.query import bind_query, parse_query, resolve_query_sources
from yodb.runtime import CatalogEvaluation, SourceRuntimeState, SourceRuntimeStatus


_NOW = datetime(2026, 9, 17, tzinfo=UTC)


class FederatedCostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.active = _active_catalog()
        self.planner = FederatedPhysicalPlanner(
            SourceCapabilityRegistry([(SourceKind.POSTGRES, PostgresSourceCapabilities())])
        )
        self.estimator = FederatedPlanEstimator(CostEstimatorRegistry([PostgresCostEstimator()]))

    def test_selective_anchor_key_transfer_is_cheaper_than_broad_contributor_scan(self) -> None:
        resolved = _resolved_query(self.active)

        candidates = self.planner.enumerate(resolved)
        assessments = self.estimator.assess_all(candidates, self.active)
        by_strategy = {assessment.candidate.strategy: assessment for assessment in assessments}
        baseline = by_strategy["remote_scans_then_assembly"]
        transferred = by_strategy["key_transfer:crm_postgres->billing_postgres"]

        self.assertLess(transferred.estimate.transfer_bytes.expected, baseline.estimate.transfer_bytes.expected)
        # Anchor CRM scan is shared by the key set and record assembly; it is
        # not incorrectly counted as another remote query.
        self.assertEqual(transferred.estimate.remote_calls.expected, 2)
        self.assertEqual(
            [name for name, _ in transferred.stages].count("remote_scan:crm_postgres"),
            1,
        )
        decision = FederatedCostPolicy().decide(
            assessments,
            FederatedQueryBudget(
                maximum_key_count=5_000,
                maximum_key_bytes=250_000,
                maximum_transfer_bytes=10_000_000,
                maximum_coordinator_memory_bytes=10_000_000,
            ),
        )
        assert decision.selected is not None
        self.assertEqual(decision.selected.candidate.strategy, "key_transfer:crm_postgres->billing_postgres")

    def test_key_budget_rejects_transfer_but_not_the_broad_candidate(self) -> None:
        assessments = self.estimator.assess_all(self.planner.enumerate(_resolved_query(self.active)), self.active)

        decision = FederatedCostPolicy().decide(
            assessments,
            FederatedQueryBudget(maximum_key_count=100, maximum_transfer_bytes=1_000_000_000),
        )

        rejected = {assessment.candidate.strategy: reasons for assessment, reasons in decision.rejected}
        self.assertIn("key_transfer:crm_postgres->billing_postgres", rejected)
        self.assertEqual(rejected["key_transfer:crm_postgres->billing_postgres"][0].code, "MAXIMUM_KEY_COUNT")
        assert decision.selected is not None
        self.assertEqual(decision.selected.candidate.strategy, "remote_scans_then_assembly")

    def test_cross_source_disjunction_is_not_split_or_pushed(self) -> None:
        query = bind_query(
            parse_query(
                {
                    "from": {"dataset": "customer"},
                    "select": ["name", "plan"],
                    "where": {
                        "any": [
                            {"field": "status", "op": "eq", "value": "active"},
                            {"field": "plan", "op": "eq", "value": "enterprise"},
                        ]
                    },
                }
            ),
            self.active,
        )

        baseline = self.planner.enumerate(resolve_query_sources(query, self.active))[0].plan
        self.assertIsNone(baseline.anchor.pushed_where)
        self.assertIsNone(baseline.contributors[0].pushed_where)


def _resolved_query(active: CatalogEvaluation):
    query = bind_query(
        parse_query(
            {
                "from": {"dataset": "customer"},
                "select": ["name", "plan"],
                "where": {"field": "status", "op": "eq", "value": "active"},
            }
        ),
        active,
    )
    return resolve_query_sources(query, active)


def _active_catalog() -> CatalogEvaluation:
    catalog = Catalog(
        metadata=CatalogMetadata(name="cost", version=1),
        datasets={
            "customer": DatasetSpec(
                description="A customer.",
                fields={
                    "id": FieldSpec(type=LogicalType.ID, description="Stable identity."),
                    "name": FieldSpec(type=LogicalType.STRING, description="Customer name."),
                    "status": FieldSpec(type=LogicalType.STRING, description="Lifecycle status."),
                    "plan": FieldSpec(type=LogicalType.STRING, description="Billing plan."),
                },
            )
        },
        sources={
            "crm_postgres": SourceSpec(
                kind=SourceKind.POSTGRES,
                connection_ref="secret://crm",
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
                connection_ref="secret://billing",
                read_only=True,
                datasets={
                    "customer": SourceDatasetSpec(
                        resource="billing.customers",
                        identity=("id",),
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
    inspections = {
        "crm_postgres": _inspection(
            "crm_postgres",
            "public.accounts",
            1_000,
            {
                "account_uuid": PhysicalField(name="account_uuid", native_type="uuid", type_family="uuid", estimated_distinct_values=1_000, average_value_bytes=16),
                "company_name": PhysicalField(name="company_name", native_type="text", type_family="string", estimated_distinct_values=900, average_value_bytes=24),
                "account_status": PhysicalField(name="account_status", native_type="text", type_family="string", estimated_distinct_values=2, average_value_bytes=8),
            },
            "account_uuid",
        ),
        "billing_postgres": _inspection(
            "billing_postgres",
            "billing.customers",
            1_000_000,
            {
                "crm_customer_uuid": PhysicalField(name="crm_customer_uuid", native_type="uuid", type_family="uuid", estimated_distinct_values=1_000_000, average_value_bytes=16),
                "subscription_plan": PhysicalField(name="subscription_plan", native_type="text", type_family="string", estimated_distinct_values=4, average_value_bytes=12),
            },
            "crm_customer_uuid",
        ),
    }
    return CatalogEvaluation(
        catalog=catalog,
        evaluated_at=_NOW,
        sources={
            name: SourceRuntimeState(
                source_name=name,
                status=SourceRuntimeStatus.VALID,
                inspection=inspection,
                validation=SourceValidationReport(source_name=name, inspected_at=_NOW),
            )
            for name, inspection in inspections.items()
        },
    )


def _inspection(source_name: str, resource_name: str, rows: int, fields: dict[str, PhysicalField], identity: str) -> SourceInspection:
    return SourceInspection(
        source_name=source_name,
        source_kind=SourceKind.POSTGRES,
        inspected_at=_NOW,
        resources={
            resource_name: PhysicalResource(
                name=resource_name,
                kind=ResourceKind.TABLE,
                fields=fields,
                primary_key=PhysicalKey(name=f"{source_name}_pkey", fields=(identity,)),
                indexes=(PhysicalIndex(name=f"{source_name}_id_idx", fields=(identity,), valid=True),),
                estimated_rows=rows,
                average_row_bytes=100,
            )
        },
    )
