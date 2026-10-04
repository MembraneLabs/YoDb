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
from yodb.planning import (
    CoordinatorFilter,
    CoordinatorSortPage,
    FederatedPhysicalPlanner,
    PostgresPlanningAdapter,
    RecordAssembly,
    RemoteScan,
    ResultProject,
    SourcePlanningRegistry,
)
from yodb.query import bind_query, parse_query, resolve_query_sources
from yodb.runtime import CatalogEvaluation, SourceRuntimeState, SourceRuntimeStatus
from yodb.inspection import SourceInspection, SourceValidationReport


_NOW = datetime(2026, 9, 17, tzinfo=UTC)


class FederatedPhysicalPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.active = _active_catalog()
        self.planner = FederatedPhysicalPlanner(SourcePlanningRegistry([PostgresPlanningAdapter()]))

    def test_multi_source_and_pushes_independent_conjuncts_without_residual(self) -> None:
        planned = self._plan(
            {
                "from": {"dataset": "customer"},
                "select": ["name", "plan"],
                "where": {
                    "all": [
                        {"field": "status", "op": "eq", "value": "active"},
                        {"field": "plan", "op": "eq", "value": "enterprise"},
                    ]
                },
                "order_by": [{"field": "name", "direction": "asc"}],
                "page": {"first": 10},
            }
        )

        self.assertIsInstance(planned.plan, ResultProject)
        sort = planned.plan.input
        self.assertIsInstance(sort, CoordinatorSortPage)
        # Every conjunct was accepted by its owning source, so no residual.
        assembly = sort.input
        self.assertIsInstance(assembly, RecordAssembly)
        self.assertEqual(assembly.required_contributor_matches, ("billing",))
        self.assertEqual(assembly.maximum_transfer_keys, 1_000)
        scans = (assembly.anchor, *assembly.contributors)
        self.assertEqual([scan.source.source_name for scan in scans], ["crm", "billing"])
        self.assertEqual([scan.pushed_filter.field.name for scan in scans], ["status", "plan"])
        self.assertEqual([scan.pushed_filter.value for scan in scans], ["active", "enterprise"])
        self.assertTrue(all(scan.maximum_rows == 10_000 for scan in scans))
        self.assertTrue(all(scan.limit is None and not scan.order_by for scan in scans))
        self.assertTrue(all(scan.projection[0].field.name == "id" for scan in scans))
        self.assertEqual(planned.plan_fingerprint, self._plan({
            "from": {"dataset": "customer"}, "select": ["name", "plan"],
            "where": {"all": [{"field": "status", "op": "eq", "value": "other"}, {"field": "plan", "op": "eq", "value": "other"}]},
            "order_by": [{"field": "name", "direction": "asc"}], "page": {"first": 10},
        }).plan_fingerprint)

    def test_cross_source_or_is_never_split_and_global_sort_page_remains_local(self) -> None:
        planned = self._plan(
            {
                "from": {"dataset": "customer"},
                "select": ["name", "plan"],
                "where": {"any": [
                    {"field": "status", "op": "eq", "value": "active"},
                    {"field": "plan", "op": "eq", "value": "enterprise"},
                ]},
                "order_by": [{"field": "name", "direction": "desc"}],
                "page": {"first": 2},
            }
        )
        assembly = planned.plan.input.input.input
        self.assertIsInstance(assembly, RecordAssembly)
        self.assertTrue(all(scan.pushed_filter is None for scan in (assembly.anchor, *assembly.contributors)))
        self.assertEqual(planned.explain.nodes[-2].kind, "coordinator_sort_page")
        self.assertEqual(planned.explain.nodes[-2].limit, 2)

    def test_single_source_pushes_complete_filter_order_and_page(self) -> None:
        planned = self._plan(
            {
                "from": {"dataset": "customer"},
                "select": ["name", "status"],
                "where": {"any": [
                    {"field": "status", "op": "eq", "value": "active"},
                    {"field": "status", "op": "eq", "value": "pending"},
                ]},
                "order_by": [{"field": "name", "direction": "asc"}],
                "page": {"first": 3},
            }
        )
        scan = planned.plan.input.input  # whole filter pushed: no coordinator filter
        self.assertIsInstance(scan, RemoteScan)
        self.assertEqual(scan.source.source_name, "crm")
        self.assertIsNotNone(scan.pushed_filter)
        self.assertEqual(scan.limit, 3)
        self.assertEqual([term.field.name for term in scan.order_by], ["name", "id"])

    def _plan(self, raw: dict[str, object]):
        query = bind_query(parse_query(raw), self.active)
        return self.planner.plan(resolve_query_sources(query, self.active))


def _active_catalog() -> CatalogEvaluation:
    customer = DatasetSpec(
        description="Customer.",
        fields={
            "id": FieldSpec(type=LogicalType.ID, description="ID."),
            "name": FieldSpec(type=LogicalType.STRING, description="Name."),
            "status": FieldSpec(type=LogicalType.STRING, description="Status."),
            "plan": FieldSpec(type=LogicalType.STRING, description="Plan."),
        },
    )
    catalog = Catalog(
        metadata=CatalogMetadata(name="planner", version=1),
        datasets={"customer": customer},
        sources={
            "crm": SourceSpec(kind=SourceKind.POSTGRES, connection_ref="crm", read_only=True, datasets={"customer": SourceDatasetSpec(resource="crm.accounts", identity=("id",), fields={"id": SourceFieldSpec(physical_name="account_id"), "name": SourceFieldSpec(physical_name="name"), "status": SourceFieldSpec(physical_name="status")})}),
            "billing": SourceSpec(kind=SourceKind.POSTGRES, connection_ref="billing", read_only=True, datasets={"customer": SourceDatasetSpec(resource="billing.customers", identity=("id",), fields={"id": SourceFieldSpec(physical_name="customer_id"), "plan": SourceFieldSpec(physical_name="plan")})}),
        },
        resolution={"customer": DatasetResolution(identity_source="crm", field_sources={"id": "crm", "name": "crm", "status": "crm", "plan": "billing"})},
        relationships={},
    )
    return CatalogEvaluation(
        catalog=catalog,
        evaluated_at=_NOW,
        sources={name: SourceRuntimeState(source_name=name, status=SourceRuntimeStatus.VALID, inspection=SourceInspection(source_name=name, source_kind=source.kind, inspected_at=_NOW), validation=SourceValidationReport(source_name=name, inspected_at=_NOW)) for name, source in catalog.sources.items()},
    )
