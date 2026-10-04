"""Adding a database = adding an adapter: the planner plans from its declared capabilities.

``LIMITED`` is a deliberately weak fake database (kind NEO4J here, as a stand-in)
that can only filter on ``eq``/``in`` over strings, cannot order or limit, cannot
be restricted by IDs, and has no vector search.  Nothing in the planner or
executor was written for it.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass, replace

from yodb.catalog import LogicalType, SourceKind, VectorMetric
from yodb.compilation import CompiledOutputColumn, PostgresQueryCompiler, QueryCompilerRegistry
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine
from yodb.planning import (
    CapabilityPlanningAdapter,
    CoordinatorFilter,
    CoordinatorSortPage,
    FederatedPhysicalPlanner,
    KeyLookupCapability,
    POSTGRES_CAPABILITIES,
    PostgresPlanningAdapter,
    RemoteScan,
    SourceOperationRequest,
    SourcePlanningRegistry,
    VectorSearchCapability,
)
from yodb.query import bind_query, ComparisonOperator, parse_query, resolve_query_sources
from yodb.semantic import SemanticExtension, SemanticPlanKind, SemanticPolicy, SemanticVerify

from support.capabilities import KIND, LIMITED, STRINGS
from support.catalogs import crm_billing_catalog as _crm_billing_catalog, SourceRowsExecutor, StaticRuntime
from support.tickets import EMBEDDER_INFO, prio, q as ticket_query, sem, ticket_catalog as _ticket_catalog


def swap_kind(active, source_name):
    """The same catalog, but one source is now served by the other database."""

    catalog = active.catalog
    source = catalog.sources[source_name].model_copy(update={"kind": KIND})
    new_catalog = catalog.model_copy(update={"sources": {**catalog.sources, source_name: source}})
    return active.model_copy(update={"catalog": new_catalog})


def planner(*adapters, **semantic):
    return FederatedPhysicalPlanner(
        SourcePlanningRegistry(list(adapters)), extensions=(SemanticExtension(policy=SemanticPolicy(**semantic)),)
    )


def nodes(planned):
    node = planned.plan
    while True:
        yield node
        if not hasattr(node, "input"):
            return
        node = node.input


def scans_of(planned):
    node = planned.plan
    while hasattr(node, "input"):
        node = node.input
    return (node,) if isinstance(node, RemoteScan) else (node.anchor, *node.contributors)


class LimitedSourcePlanningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.active = swap_kind(_crm_billing_catalog(), "crm")  # crm is the limited database now
        cls.planner = planner(PostgresPlanningAdapter(), CapabilityPlanningAdapter(LIMITED))

    def plan(self, where=None, select=("name",), **extra):
        raw = {"from": {"dataset": "customer"}, "select": list(select), "page": {"first": 5}, **extra}
        if where is not None:
            raw["where"] = where
        return self.planner.plan(resolve_query_sources(bind_query(parse_query(raw), self.active), self.active))

    def test_operators_the_source_did_not_declare_stay_in_yodb(self) -> None:
        eq = {"field": "status", "op": "eq", "value": "a"}
        for where, pushed in (
            (eq, True),
            ({"field": "status", "op": "in", "value": ["a", "b"]}, True),
            ({"all": [eq, eq]}, True),
            ({"field": "status", "op": "ne", "value": "a"}, False),          # operator not declared
            ({"field": "status", "op": "is_null"}, False),
            ({"any": [eq, eq]}, False),                                        # ANY not declared
            ({"not": eq}, False),                                              # NOT not declared
        ):
            with self.subTest(where=where):
                planned = self.plan(where)
                (scan,) = scans_of(planned)
                self.assertEqual(scan.pushed_filter is not None, pushed)
                self.assertEqual(any(isinstance(n, CoordinatorFilter) for n in nodes(planned)), not pushed)

    def test_a_source_that_cannot_order_or_limit_gets_neither_and_is_row_capped(self) -> None:
        planned = self.plan({"field": "status", "op": "eq", "value": "a"}, order_by=[{"field": "name", "direction": "asc"}])
        (scan,) = scans_of(planned)
        self.assertEqual((scan.order_by, scan.limit), ((), None))
        self.assertEqual(scan.maximum_rows, 50)  # the source's cap beats the 10,000 default
        self.assertTrue(any(isinstance(n, CoordinatorSortPage) for n in nodes(planned)))

    def test_the_decision_explains_what_was_refused(self) -> None:
        raw = {"from": {"dataset": "customer"}, "select": ["name"], "where": {"field": "status", "op": "ne", "value": "a"}, "page": {"first": 5}}
        bound = bind_query(parse_query(raw), self.active)
        source = resolve_query_sources(bound, self.active).sources[0]
        request = SourceOperationRequest(
            projection=(source.logical_id, *source.fields), filter=bound.where, order_by=bound.order_by, limit=5, complete_result=True
        )
        decision = CapabilityPlanningAdapter(LIMITED).plan_remote_scan(source, request)
        self.assertIsNone(decision.accepted_filter)
        self.assertIs(decision.residual_filter, bound.where)
        self.assertEqual((decision.accepted_order, decision.accepted_limit), ((), None))
        self.assertIn("the source cannot filter with 'ne'", decision.reasons)


class LimitedSourceExecutionTests(unittest.TestCase):
    """Key transfer is decided per source from its declared lookup capability."""

    CRM = tuple({"id": f"c{i}", "name": f"N{i}", "status": "a"} for i in range(1, 6))
    BILLING = ({"id": "c1", "plan": "pro"}, {"id": "c3", "plan": "pro"}, {"id": "c5", "plan": "pro"})

    def run_query(self, limited_caps):
        active = swap_kind(_crm_billing_catalog(), "crm")
        scans = []

        class Compiler:
            source_kind = KIND

            def compile(self, query):  # pragma: no cover - the planner path never calls it
                raise NotImplementedError

            def compile_scan(self, scan):
                scans.append(scan)
                return LimitedCommand(scan.source.source_name, scan.source.connection_ref, tuple(
                    CompiledOutputColumn(f.field.name, f.field.name) for f in scan.projection
                ))

        class Executor:
            source_kind = KIND

            def execute(self, query, *, timeout_seconds=None):
                return LimitedSourceExecutionTests.CRM

        engine = QueryExecutionEngine(
            StaticRuntime(active),
            QueryCompilerRegistry([PostgresQueryCompiler(), Compiler()]),
            QueryExecutionAdapterRegistry([SourceRowsExecutor({"billing": self.BILLING}), Executor()]),
            planner=planner(PostgresPlanningAdapter(), CapabilityPlanningAdapter(limited_caps)),
        )
        rows = engine.execute({"from": {"dataset": "customer"}, "select": ["name", "plan"], "where": {"field": "plan", "op": "eq", "value": "pro"}, "page": {"first": 10}}).rows
        return [r["id"] for r in rows], {s.source.source_name: s for s in scans}

    def test_a_source_without_key_lookup_is_never_restricted_but_results_are_still_right(self) -> None:
        ids, scans = self.run_query(LIMITED)
        self.assertEqual(ids, ["c1", "c3", "c5"])
        self.assertIsNone(scans["crm"].key_lookup_limit)
        self.assertIsNone(scans["crm"].key_filter)

    def test_a_source_with_a_small_lookup_limit_is_restricted_only_when_the_ids_fit(self) -> None:
        fits = replace(LIMITED, key_lookup=KeyLookupCapability(3))
        ids, scans = self.run_query(fits)
        self.assertEqual(scans["crm"].key_filter, ("c1", "c3", "c5"))  # 3 IDs <= its limit of 3
        too_small = replace(LIMITED, key_lookup=KeyLookupCapability(2))
        ids2, scans2 = self.run_query(too_small)
        self.assertIsNone(scans2["crm"].key_filter)                    # 3 IDs > 2: plain scan
        self.assertEqual(ids, ids2)


@dataclass(frozen=True)
class LimitedCommand:
    source_name: str
    connection_ref: str
    output_columns: tuple
    source_kind: SourceKind = KIND


class SemanticCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.active = swap_kind(_ticket_catalog(), "helpdesk")

    def choose(self, raw, caps, **policy):
        adapters = (PostgresPlanningAdapter(), CapabilityPlanningAdapter(caps))
        planned = planner(*adapters, embedder=EMBEDDER_INFO, embedder_dimensions=3, **policy).plan(
            resolve_query_sources(bind_query(parse_query(raw), self.active), self.active)
        )
        verify = next(n for n in nodes(planned) if isinstance(n, SemanticVerify))
        notes = [d for n in planned.explain.nodes for d in n.detail if d.startswith("note:")]
        return verify, notes, scans_of(planned)

    def vector(self, **overrides):
        base = dict(metrics=frozenset({VectorMetric.COSINE}), combines_with_filters=True)
        return replace(LIMITED, vector_search=VectorSearchCapability(**{**base, **overrides}), filter_operators=LIMITED.filter_operators | {ComparisonOperator.GTE}, filterable_types=STRINGS | {LogicalType.INT})

    def test_no_declared_vector_search_means_verify_all_with_the_reason(self) -> None:
        verify, notes, _ = self.choose(ticket_query(sem()), LIMITED)
        self.assertEqual(verify.plan, SemanticPlanKind.VERIFY_ALL)
        self.assertIn("note: the source cannot do vector search", notes)

    def test_a_declared_vector_search_makes_the_shortlist_available(self) -> None:
        verify, _, (scan,) = self.choose(ticket_query(sem()), self.vector())
        self.assertEqual(verify.plan, SemanticPlanKind.VECTOR_SHORTLIST)
        self.assertEqual(scan.vector_search.metric, VectorMetric.COSINE)

    def test_an_unsupported_metric_is_refused(self) -> None:
        verify, notes, _ = self.choose(ticket_query(sem()), self.vector(metrics=frozenset({VectorMetric.L2})))
        self.assertEqual(verify.plan, SemanticPlanKind.VERIFY_ALL)
        self.assertTrue(any("cosine" in n for n in notes), notes)

    def test_ranking_that_cannot_combine_with_filters_is_used_only_for_an_unfiltered_query(self) -> None:
        caps = self.vector(combines_with_filters=False)
        self.assertEqual(self.choose(ticket_query(sem()), caps)[0].plan, SemanticPlanKind.VECTOR_SHORTLIST)
        filtered, notes, _ = self.choose(ticket_query({"all": [prio(), sem()]}), caps)
        self.assertEqual(filtered.plan, SemanticPlanKind.VERIFY_ALL)
        self.assertTrue(any("combine vector ranking" in n for n in notes), notes)
        multi, _, _ = self.choose(ticket_query(sem(), select=("subject", "owner")), caps)
        self.assertEqual(multi.plan, SemanticPlanKind.VERIFY_ALL)

    def test_the_sources_maximum_shortlist_bounds_the_search(self) -> None:
        _, _, (scan,) = self.choose(ticket_query(sem(), first=100), self.vector(maximum_shortlist=7))
        self.assertEqual(scan.vector_search.shortlist_size, 7)


class OrderingNoteTests(unittest.TestCase):
    def test_yodb_flags_text_ordering_it_does_in_place_of_a_collating_source(self) -> None:
        active = _crm_billing_catalog()  # both postgres: SOURCE_DEFINED ordering
        raw = {"from": {"dataset": "customer"}, "select": ["name", "plan"], "order_by": [{"field": "name", "direction": "asc"}], "page": {"first": 5}}
        planned = planner(PostgresPlanningAdapter()).plan(resolve_query_sources(bind_query(parse_query(raw), active), active))
        (sort,) = [n for n in planned.explain.nodes if n.kind == "coordinator_sort_page"]
        self.assertEqual(sort.detail, ("note: text is ordered by YoDb in code-point order; the owning source's collation differs for: crm",))

    def test_no_note_when_the_owning_source_orders_like_yodb_or_the_order_is_not_text(self) -> None:
        for raw_order, active in (
            ([{"field": "name", "direction": "asc"}], swap_kind(_crm_billing_catalog(), "crm")),  # CODE_POINT source
            ([{"field": "id", "direction": "asc"}], _crm_billing_catalog()),                       # not text
        ):
            raw = {"from": {"dataset": "customer"}, "select": ["name", "plan"], "order_by": raw_order, "page": {"first": 5}}
            planned = planner(PostgresPlanningAdapter(), CapabilityPlanningAdapter(LIMITED)).plan(
                resolve_query_sources(bind_query(parse_query(raw), active), active)
            )
            (sort,) = [n for n in planned.explain.nodes if n.kind == "coordinator_sort_page"]
            self.assertEqual(sort.detail, ())


class PostgresDeclarationIsKeptTests(unittest.TestCase):
    """What Postgres declares must be exactly what its compiler can do."""

    @classmethod
    def setUpClass(cls):
        cls.active = _ticket_catalog()
        cls.planner = planner(PostgresPlanningAdapter())

    def scan_for(self, where):
        raw = {"from": {"dataset": "ticket"}, "select": ["subject"], "where": where, "page": {"first": 5}}
        planned = self.planner.plan(resolve_query_sources(bind_query(parse_query(raw), self.active), self.active))
        return scans_of(planned)[0]

    def test_every_declared_filter_operator_is_pushed_and_compiles(self) -> None:
        for op in sorted(POSTGRES_CAPABILITIES.filter_operators, key=lambda o: o.value):
            where = {"field": "priority", "op": op.value}
            if op in (ComparisonOperator.IN, ComparisonOperator.NOT_IN):
                where["value"] = [1, 2]
            elif op not in (ComparisonOperator.IS_NULL, ComparisonOperator.IS_NOT_NULL):
                where["value"] = 3
            with self.subTest(op=op.value):
                scan = self.scan_for(where)
                self.assertIsNotNone(scan.pushed_filter)
                PostgresQueryCompiler().compile_scan(scan)

    def test_declared_boolean_operators_push_and_compile(self) -> None:
        a, b = ({"field": "priority", "op": "gt", "value": 1}, {"field": "subject", "op": "eq", "value": "x"})
        for where in ({"all": [a, b]}, {"any": [a, b]}, {"not": a}):
            with self.subTest(where=list(where)):
                scan = self.scan_for(where)
                self.assertIsNotNone(scan.pushed_filter)
                PostgresQueryCompiler().compile_scan(scan)

    def test_operators_postgres_did_not_declare_are_not_pushed(self) -> None:
        for op in (ComparisonOperator.CONTAINS, ComparisonOperator.STARTS_WITH):
            self.assertNotIn(op, POSTGRES_CAPABILITIES.filter_operators)
            self.assertIsNone(self.scan_for({"field": "subject", "op": op.value, "value": "x"}).pushed_filter)


if __name__ == "__main__":
    unittest.main()
