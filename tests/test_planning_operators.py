"""Planning operators: each does one job, and a new extension needs no planner change."""

from __future__ import annotations

import unittest

from yodb.catalog import SourceKind
from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.errors import ErrorCode, QueryError
from yodb.execution import QueryExecutionAdapterRegistry
from yodb.execution.federated import FederatedPlanExecutor
from yodb.planning import (
    FederatedPhysicalPlanner,
    PostgresPlanningAdapter,
    RemoteScan,
    ScanOperator,
    SourcePlanningRegistry,
    SourceStatistics,
    StatisticsService,
)

from support.catalogs import SourceRowsExecutor
from support.statistics import MapProvider
from support.tickets import DIRECTORY, HELPDESK, planning_services, prio, q, resolve, sem
from support.toys import KeepEqual, Plugin, SubjectToy


OWNER = {"field": "owner", "op": "eq", "value": "ann"}


def services(**kw):
    return planning_services(**kw)


class ScanOperatorTests(unittest.TestCase):
    def scan(self, raw, **kw):
        return ScanOperator(services()).plan(resolve(raw), **kw)

    def test_a_single_source_reports_what_it_enforced(self) -> None:
        result = self.scan(q(prio(), select=("subject",)))
        self.assertEqual((len(result.scans), result.fully_pushed, result.page_pushed), (1, True, True))

    def test_a_filter_the_source_refuses_is_not_fully_pushed_and_not_page_pushed(self) -> None:
        result = self.scan(q({"field": "subject", "op": "contains", "value": "x"}, select=("subject",)))
        self.assertEqual((result.fully_pushed, result.page_pushed), (False, False))
        self.assertIsNone(result.scans[0].pushed_filter)
        self.assertEqual(result.scans[0].maximum_rows, 10_000)

    def test_multi_source_never_pages_at_a_source_and_pushes_each_conjunct(self) -> None:
        result = self.scan(q({"all": [OWNER, prio()]}, select=("subject", "owner")))
        self.assertEqual([s.source.source_name for s in result.scans], ["helpdesk", "directory"])
        self.assertTrue(all(s.pushed_filter is not None for s in result.scans))
        self.assertEqual((result.fully_pushed, result.page_pushed), (True, False))

    def test_allow_complete_false_keeps_a_single_source_from_pushing_order_and_limit(self) -> None:
        result = self.scan(q(prio(), select=("subject",), first=3), allow_complete=False)
        scan = result.scans[0]
        self.assertEqual((scan.limit, scan.order_by, result.page_pushed), (None, (), False))
        self.assertIsNotNone(scan.maximum_rows)


class PlannerStructureTests(unittest.TestCase):
    def test_with_no_extensions_a_semantic_term_is_not_silently_ignored(self) -> None:
        planner = FederatedPhysicalPlanner(SourcePlanningRegistry([PostgresPlanningAdapter()]))
        with self.assertRaises(QueryError) as caught:
            planner.plan(resolve(q(sem())))   # the spine cannot plan a term nobody claimed
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_FEATURE_NOT_SUPPORTED)
        self.assertEqual(caught.exception.detail.message, "The query has a semantic condition, but nothing is configured to answer one.")


def _walk(node):
    yield node
    for child in node.inputs():
        yield from _walk(child)


# --- a brand-new extension operator: nothing in the planner, optimizer or executor changes ---------


class NewExtensionTests(unittest.TestCase):
    def planner(self, statistics=None):
        return FederatedPhysicalPlanner(
            SourcePlanningRegistry([PostgresPlanningAdapter()]), statistics=statistics, extensions=(Plugin(SubjectToy()),)
        )

    def statistics(self):
        provider = MapProvider({"public.tickets": SourceStatistics(row_count=10_000), "public.owners": SourceStatistics(row_count=10_000)})
        return StatisticsService({SourceKind.POSTGRES: provider})

    def toy_query(self):
        return q({"all": [prio(), {"field": "subject", "op": "eq", "value": "S2"}]}, select=("subject",))

    def mode(self, planned):
        return next(n for n in _walk(planned.plan) if isinstance(n, KeepEqual)).mode

    def test_without_statistics_the_fixed_rule_default_is_used(self) -> None:
        planned = self.planner().plan(resolve(self.toy_query()))
        self.assertEqual(self.mode(planned), "slow")
        self.assertEqual(planned.explain.optimizer[0], "strategy=rules")

    def test_with_statistics_the_optimizer_picks_the_cheaper_variant_it_knows_nothing_about(self) -> None:
        planned = self.planner(self.statistics()).plan(resolve(self.toy_query()))
        self.assertEqual(self.mode(planned), "cheap")
        self.assertEqual(planned.explain.optimizer[0], "strategy=cost_based")
        self.assertEqual(self.mode(planned), "cheap")

    def test_the_explanation_and_fingerprint_include_the_new_node_without_any_change(self) -> None:
        planned = self.planner(self.statistics()).plan(resolve(self.toy_query()))
        kinds = [entry.kind for entry in planned.explain.nodes]
        self.assertIn("keep_equal", kinds)
        slow = self.planner().plan(resolve(self.toy_query()))
        self.assertNotEqual(planned.plan_fingerprint, slow.plan_fingerprint)

    def test_the_executor_runs_it_through_a_registered_handler(self) -> None:
        planned = self.planner(self.statistics()).plan(resolve(self.toy_query()))
        rows = SourceRowsExecutor({"helpdesk": HELPDESK, "directory": DIRECTORY})
        executor = FederatedPlanExecutor(
            QueryCompilerRegistry([PostgresQueryCompiler()]), QueryExecutionAdapterRegistry([rows]), extensions=(Plugin(SubjectToy()),)
        )
        result = executor.execute(planned.plan)
        self.assertEqual([r["subject"] for r in result], ["S2"])

    def test_the_remaining_filter_is_still_planned_by_the_spine(self) -> None:
        planned = self.planner().plan(resolve(self.toy_query()))
        scan = next(n for n in _walk(planned.plan) if isinstance(n, RemoteScan))
        self.assertEqual(scan.pushed_filter.field.name, "priority")


if __name__ == "__main__":
    unittest.main()
