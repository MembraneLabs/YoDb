"""Planner/executor path and edge-case tests (no database; fake source rows).

Catalog: ``customer`` with crm (id, name, status) as identity source and
billing (id, plan) as contributor.
"""

from __future__ import annotations

import unittest

from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.errors import ErrorCode, QueryError, QueryExecutionError
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine
from yodb.execution.federated import FederatedExecutionPolicy, FederatedPlanExecutor
from yodb.planning import (
    FederatedPhysicalPlanner,
    PostgresPlanningAdapter,
    RecordAssembly,
    RemoteScan,
    SourcePlanningRegistry,
)
from yodb.query import bind_query, parse_query, resolve_query_sources

from test_execution import SourceRowsExecutor, StaticRuntime, _multi_active_catalog
from test_planning import _active_catalog as _planning_catalog  # same shape as _multi_active_catalog

CUSTOMER = {"from": {"dataset": "customer"}}


def _q(select, where=None, order_by=None, first=10, **page):
    query = {**CUSTOMER, "select": select, "page": {"first": first, **page}}
    if where is not None:
        query["where"] = where
    if order_by is not None:
        query["order_by"] = order_by
    return query


def _eq(field, value):
    return {"field": field, "op": "eq", "value": value}


def _scans(planned):
    node = planned.plan
    while not isinstance(node, (RemoteScan, RecordAssembly)):
        node = node.input
    return (node,) if isinstance(node, RemoteScan) else (node.anchor, *node.contributors)


def _assembly(planned):
    node = planned.plan
    while not isinstance(node, (RemoteScan, RecordAssembly)):
        node = node.input
    return node


class PlanShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.active = _planning_catalog()
        self.planner = FederatedPhysicalPlanner(SourcePlanningRegistry([PostgresPlanningAdapter()]))

    def plan(self, raw):
        return self.planner.plan(resolve_query_sources(bind_query(parse_query(raw), self.active), self.active))

    def test_fully_pushable_single_source_has_no_row_guard(self) -> None:
        (scan,) = _scans(self.plan(_q(["name"], _eq("status", "active"))))
        self.assertIsNotNone(scan.pushed_filter)
        self.assertIsNone(scan.maximum_rows)

    def test_unsupported_operator_keeps_filter_order_limit_local_and_guards_rows(self) -> None:
        planned = self.plan(
            _q(["name"], {"field": "name", "op": "contains", "value": "acme"}, [{"field": "name", "direction": "asc"}], 5)
        )
        (scan,) = _scans(planned)
        self.assertIsNone(scan.pushed_filter)
        self.assertEqual(scan.order_by, ())
        self.assertIsNone(scan.limit)
        self.assertEqual(scan.maximum_rows, 10_000)

    def test_caller_touching_only_identity_source_does_not_involve_billing(self) -> None:
        planned = self.plan(_q(["name", "status"], _eq("status", "active")))
        self.assertEqual([s.source.source_name for s in _scans(planned)], ["crm"])
        self.assertNotIsInstance(_assembly(planned), RecordAssembly)

    def test_only_contributor_field_used_still_anchors_on_identity_source(self) -> None:
        planned = self.plan(_q(["plan"], _eq("plan", "enterprise")))
        assembly = _assembly(planned)
        self.assertIsInstance(assembly, RecordAssembly)
        self.assertEqual(assembly.anchor.source.source_name, "crm")
        self.assertEqual(assembly.required_contributor_matches, ("billing",))
        for scan in (assembly.anchor, *assembly.contributors):
            self.assertEqual(scan.projection[0].field.name, "id")

    def test_contributor_without_filter_is_not_a_required_match(self) -> None:
        assembly = _assembly(self.plan(_q(["name", "plan"], _eq("status", "active"))))
        self.assertEqual(assembly.required_contributor_matches, ())
        self.assertIsNotNone(assembly.anchor.pushed_filter)
        self.assertIsNone(assembly.contributors[0].pushed_filter)

    def test_contributor_is_null_is_never_pushed_or_required(self) -> None:
        assembly = _assembly(self.plan(_q(["plan"], {"field": "plan", "op": "is_null"})))
        self.assertIsNone(assembly.contributors[0].pushed_filter)
        self.assertEqual(assembly.required_contributor_matches, ())

    def test_anchor_is_null_is_still_pushed(self) -> None:
        (scan,) = _scans(self.plan(_q(["name"], {"field": "name", "op": "is_null"})))
        self.assertIsNotNone(scan.pushed_filter)

    def test_cross_source_not_and_nested_or_are_never_pushed(self) -> None:
        for where in (
            {"not": _eq("plan", "basic")},
            {"all": [_eq("status", "active"), {"any": [_eq("status", "x"), _eq("plan", "y")]}]},
        ):
            with self.subTest(where=where):
                self.assertTrue(all(s.pushed_filter is None for s in _scans(self.plan(_q(["name", "plan"], where)))))

    def test_multi_source_never_pushes_order_or_limit(self) -> None:
        planned = self.plan(_q(["name", "plan"], _eq("plan", "x"), [{"field": "name", "direction": "asc"}], 3))
        for scan in _scans(planned):
            self.assertEqual((scan.order_by, scan.limit, scan.maximum_rows), ((), None, 10_000))

    def test_cursor_is_rejected_until_signed_cursors_exist(self) -> None:
        with self.assertRaises(QueryError) as caught:
            self.plan(_q(["name"], after="abc"))
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_FEATURE_NOT_SUPPORTED)

    def test_fingerprint_ignores_values_but_not_shape(self) -> None:
        a = self.plan(_q(["name"], _eq("status", "a")))
        b = self.plan(_q(["name"], _eq("status", "b")))
        c = self.plan(_q(["name"], {"field": "status", "op": "ne", "value": "a"}))
        self.assertEqual(a.plan_fingerprint, b.plan_fingerprint)
        self.assertNotEqual(a.plan_fingerprint, c.plan_fingerprint)


class ExecutionTests(unittest.TestCase):
    CRM = (
        {"id": "c1", "name": "Zulu", "status": "active"},
        {"id": "c2", "name": "Alpha", "status": "inactive"},
        {"id": "c3", "name": "Bravo", "status": "active"},
    )
    BILLING = (
        {"id": "c1", "plan": "basic"},
        {"id": "c3", "plan": "enterprise"},
        {"id": "c9", "plan": "enterprise"},  # no CRM record
    )

    def run_query(self, raw, *, crm=None, billing=None):
        executor = SourceRowsExecutor({"crm": self.CRM if crm is None else crm, "billing": self.BILLING if billing is None else billing})
        engine = QueryExecutionEngine(
            StaticRuntime(_multi_active_catalog()),
            QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([executor]),
        )
        self.executor = executor
        return [dict(row) for row in engine.execute(raw).rows]

    def ids(self, raw, **kwargs):
        return [row["id"] for row in self.run_query(raw, **kwargs)]

    # --- assembly semantics -------------------------------------------------

    def test_left_enrichment_gives_null_for_missing_contributor_row_and_ignores_contributor_only_ids(self) -> None:
        rows = self.run_query(_q(["name", "plan"], order_by=[{"field": "name", "direction": "asc"}]))
        self.assertEqual([(r["id"], r["plan"]) for r in rows], [("c2", None), ("c3", "enterprise"), ("c1", "basic")])

    def test_contributor_filter_drops_anchor_rows_the_contributor_did_not_return(self) -> None:
        self.assertEqual(self.ids(_q(["plan"], _eq("plan", "enterprise"))), ["c3"])

    def test_or_across_sources_matches_either_side(self) -> None:
        where = {"any": [_eq("status", "inactive"), _eq("plan", "enterprise")]}
        self.assertEqual(sorted(self.ids(_q(["name", "plan"], where))), ["c2", "c3"])

    def test_not_across_sources_is_three_valued(self) -> None:
        # c2 has no billing row -> plan is NULL -> NOT(plan = 'basic') is NULL -> excluded
        self.assertEqual(self.ids(_q(["plan"], {"not": _eq("plan", "basic")})), ["c3"])

    def test_contributor_is_null_matches_anchor_rows_missing_from_contributor(self) -> None:
        # c2 has no billing row, so its plan is logically NULL and must match;
        # c1/c3 have non-null plans and must not.
        self.assertEqual(self.ids(_q(["plan"], {"field": "plan", "op": "is_null"})), ["c2"])
        # A billing row with a NULL plan matches too.
        billing = ({"id": "c1", "plan": None}, {"id": "c3", "plan": "enterprise"})
        self.assertEqual(sorted(self.ids(_q(["plan"], {"field": "plan", "op": "is_null"}), billing=billing)), ["c1", "c2"])

    # --- coordinator re-check, projection, ordering --------------------------

    def test_coordinator_rechecks_pushed_filters_even_if_source_ignores_them(self) -> None:
        # The fake source ignores WHERE and returns every row.
        self.assertEqual(sorted(self.ids(_q(["name"], _eq("status", "active")))), ["c1", "c3"])

    def test_filter_only_fields_are_not_leaked_into_results(self) -> None:
        for row in self.run_query(_q(["name"], _eq("status", "active"))):
            self.assertEqual(set(row), {"id", "name"})

    def test_contains_and_starts_with_run_case_sensitively_at_coordinator(self) -> None:
        crm = ({"id": "a", "name": "Acme Corp", "status": "x"}, {"id": "b", "name": "acme inc", "status": "x"})
        contains = {"field": "name", "op": "contains", "value": "Acme"}
        starts = {"field": "name", "op": "starts_with", "value": "acme"}
        self.assertEqual(self.ids(_q(["name"], contains), crm=crm), ["a"])
        self.assertEqual(self.ids(_q(["name"], starts), crm=crm), ["b"])

    def test_null_comparisons_never_match_but_is_null_does(self) -> None:
        crm = ({"id": "a", "name": None, "status": "x"}, {"id": "b", "name": "B", "status": "x"})
        self.assertEqual(self.ids(_q(["name"], {"field": "name", "op": "ne", "value": "Z"}), crm=crm), ["b"])
        self.assertEqual(self.ids(_q(["name"], {"field": "name", "op": "is_null"}), crm=crm), ["a"])

    def test_order_places_nulls_like_postgres_and_breaks_ties_by_id(self) -> None:
        crm = (
            {"id": "d", "name": None, "status": "x"},
            {"id": "b", "name": "Same", "status": "x"},
            {"id": "a", "name": "Same", "status": "x"},
            {"id": "c", "name": "Low", "status": "x"},
        )
        asc = self.ids(_q(["name"], order_by=[{"field": "name", "direction": "asc"}]), crm=crm, billing=())
        desc = self.ids(_q(["name"], order_by=[{"field": "name", "direction": "desc"}]), crm=crm, billing=())
        self.assertEqual(asc, ["c", "a", "b", "d"])   # NULLS LAST
        self.assertEqual(desc, ["d", "a", "b", "c"])  # NULLS FIRST, id ASC tiebreak

    def test_global_page_applies_after_assembly_and_filtering(self) -> None:
        # Per-source limits would have cut c3 off; global paging must not.
        where = _eq("plan", "enterprise")
        self.assertEqual(self.ids(_q(["name", "plan"], where, [{"field": "name", "direction": "desc"}], 1)), ["c3"])

    # --- guards and invariants ------------------------------------------------

    def test_scan_over_cap_fails_and_sql_asks_for_exactly_one_extra_row(self) -> None:
        crm = tuple({"id": f"c{i}", "name": "n", "status": "x"} for i in range(10_001))
        with self.assertRaises(QueryExecutionError) as caught:
            self.run_query(_q(["name"], {"field": "name", "op": "contains", "value": "n"}), crm=crm)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_ROW_LIMIT_EXCEEDED)
        self.assertEqual(self.executor.queries[0].parameters[-1], 10_001)

    def test_scan_exactly_at_cap_succeeds(self) -> None:
        crm = tuple({"id": f"c{i}", "name": "n", "status": "x"} for i in range(10_000))
        self.assertEqual(len(self.run_query(_q(["name"], {"field": "name", "op": "contains", "value": "n"}, first=50), crm=crm)), 50)

    def test_duplicate_or_null_ids_from_a_source_are_rejected(self) -> None:
        for crm, billing in (
            (None, ({"id": "c1", "plan": "a"}, {"id": "c1", "plan": "b"})),
            (({"id": None, "name": "n", "status": "x"},), None),
        ):
            with self.subTest(crm=crm, billing=billing), self.assertRaises(QueryExecutionError) as caught:
                self.run_query(_q(["name", "plan"]), crm=crm, billing=billing)
            self.assertEqual(caught.exception.code, ErrorCode.QUERY_PLAN_INVARIANT_VIOLATION)

    def test_coordinator_row_cap_applies_to_sort_stage(self) -> None:
        active = _multi_active_catalog()
        planner = FederatedPhysicalPlanner(SourcePlanningRegistry([PostgresPlanningAdapter()]))
        planned = planner.plan(resolve_query_sources(bind_query(parse_query(_q(["name"])), active), active))
        executor = FederatedPlanExecutor(
            QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([SourceRowsExecutor({"crm": self.CRM})]),
            policy=FederatedExecutionPolicy(maximum_coordinator_rows=2),
        )
        with self.assertRaises(QueryExecutionError) as caught:
            executor.execute(planned.plan)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_COORDINATOR_LIMIT_EXCEEDED)

    # --- engine surface -------------------------------------------------------

    def test_each_scan_gets_the_caller_timeout(self) -> None:
        timeouts = []

        class Recording(SourceRowsExecutor):
            def execute(self, query, *, timeout_seconds=None):
                timeouts.append(timeout_seconds)
                return super().execute(query, timeout_seconds=timeout_seconds)

        engine = QueryExecutionEngine(
            StaticRuntime(_multi_active_catalog()),
            QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([Recording({"crm": self.CRM, "billing": self.BILLING})]),
        )
        engine.execute(_q(["name", "plan"]), timeout_seconds=1.5)
        self.assertEqual(timeouts, [1.5, 1.5])

    def test_explain_does_not_execute_anything(self) -> None:
        executor = SourceRowsExecutor({})
        engine = QueryExecutionEngine(
            StaticRuntime(_multi_active_catalog()),
            QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([executor]),
        )
        explanation = engine.explain(_q(["name", "plan"], _eq("plan", "SECRET-VALUE")))
        self.assertEqual(executor.queries, [])
        self.assertEqual(
            [node.kind for node in explanation.nodes],
            ["remote_scan", "remote_scan", "record_assembly", "coordinator_filter", "coordinator_sort_page", "result_project"],
        )
        self.assertNotIn("SECRET-VALUE", repr(explanation))  # values are redacted


if __name__ == "__main__":
    unittest.main()
