from __future__ import annotations

import unittest

from yodb.planning import (
    CoordinatorSortPage,
    FederatedPhysicalPlanner,
    PostgresPlanningAdapter,
    RecordAssembly,
    RemoteScan,
    ResultProject,
    SourcePlanningRegistry,
)
from yodb.query import bind_query, parse_query, resolve_query_sources

from support.catalogs import crm_billing_catalog


class FederatedPhysicalPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.active = crm_billing_catalog()
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
        scan = planned.plan.input  # filter, order and page all pushed: no coordinator nodes
        self.assertIsInstance(scan, RemoteScan)
        self.assertEqual(scan.source.source_name, "crm")
        self.assertIsNotNone(scan.pushed_filter)
        self.assertEqual(scan.limit, 3)
        self.assertEqual([term.field.name for term in scan.order_by], ["name", "id"])

    def _plan(self, raw: dict[str, object]):
        query = bind_query(parse_query(raw), self.active)
        return self.planner.plan(resolve_query_sources(query, self.active))
