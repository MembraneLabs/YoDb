"""The operator catalog and what each adapter's declaration means for it."""

from __future__ import annotations

import unittest
from dataclasses import dataclass, replace

from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.execution import QueryExecutionAdapterRegistry
from yodb.execution.federated import FederatedPlanExecutor
from yodb.execution.operators import default_handlers, ExecutionContext, Run
from yodb.operators import OperatorCatalog, OperatorCategory, OperatorKind, OPERATORS
from yodb.planning import (
    CoordinatorFilter,
    CoordinatorSortPage,
    describe_capabilities,
    explain_plan,
    operator_support,
    PhysicalNode,
    plan_fingerprint,
    PlanExplanationNode,
    POSTGRES_CAPABILITIES,
    RecordAssembly,
    RemoteScan,
    ResultProject,
    SupportLevel,
    transform_plan,
    UnaryNode,
)
from yodb.semantic import SemanticExtension, SemanticPolicy, SemanticVerify

from support.capabilities import LIMITED
from support.catalogs import SourceRowsExecutor
from support.tickets import DIRECTORY, EMBEDDER_INFO, HELPDESK, OWNER, plan as ticket_plan, q, sem


K, S = OperatorKind, SupportLevel


def by_kind(capabilities):
    return {item.kind: item for item in operator_support(capabilities)}


class CatalogTests(unittest.TestCase):
    def test_every_operator_kind_has_exactly_one_spec(self) -> None:
        self.assertEqual({spec.kind for spec in OPERATORS}, set(OperatorKind))
        self.assertEqual(len(list(OPERATORS)), len(OperatorKind))

    def test_the_relational_set_goes_beyond_filter(self) -> None:
        relational = {spec.kind for spec in OPERATORS.in_category(OperatorCategory.RELATIONAL)}
        self.assertTrue({K.SCAN, K.FILTER, K.PROJECT, K.ORDER, K.LIMIT, K.COMBINE, K.JOIN, K.AGGREGATE, K.GROUP_BY, K.DISTINCT, K.UNION} <= relational)

    def test_what_exists_today_is_marked_implemented_and_the_rest_planned(self) -> None:
        self.assertEqual(
            {s.kind for s in OPERATORS.implemented()},
            {K.SCAN, K.FILTER, K.PROJECT, K.ORDER, K.LIMIT, K.COMBINE, K.KEY_LOOKUP, K.VECTOR_SEARCH, K.SEMANTIC_FILTER},
        )
        self.assertEqual({s.kind for s in OPERATORS.planned()}, {K.JOIN, K.AGGREGATE, K.GROUP_BY, K.DISTINCT, K.UNION, K.TRAVERSE})

    def test_implemented_operators_name_strategies_and_planned_ones_do_not(self) -> None:
        for spec in OPERATORS:
            self.assertEqual(bool(spec.strategies), spec.implemented, spec.kind)
        self.assertEqual(OPERATORS.get(K.SEMANTIC_FILTER).strategies, ("verify_all", "vector_shortlist"))

    def test_dependencies_between_operators_are_recorded(self) -> None:
        self.assertEqual(OPERATORS.get(K.LIMIT).requires, (K.ORDER,))
        self.assertEqual(OPERATORS.get(K.GROUP_BY).requires, (K.AGGREGATE,))

    def test_an_inconsistent_catalog_is_rejected(self) -> None:
        specs = list(OPERATORS)
        spec = OPERATORS.get(K.SCAN)
        for bad in (
            specs[1:],                                                              # a kind without a spec
            [replace(spec, strategies=())] + specs[1:],                             # implemented, no strategies
            [replace(OPERATORS.get(K.JOIN), strategies=("x",))] + [s for s in specs if s.kind is not K.JOIN],  # planned, claims strategies
            [replace(spec, requires=(K.SCAN,))] + specs[1:],                        # requires itself
        ):
            with self.assertRaises(ValueError):
                OperatorCatalog(bad)


class PostgresViewTests(unittest.TestCase):
    def test_the_view_covers_every_operator_once(self) -> None:
        view = operator_support(POSTGRES_CAPABILITIES)
        self.assertEqual([item.kind for item in view], [spec.kind for spec in OPERATORS])

    def test_postgres_runs_the_relational_basics_itself_and_yodb_combines_and_verifies(self) -> None:
        view = by_kind(POSTGRES_CAPABILITIES)
        for kind in (K.SCAN, K.FILTER, K.PROJECT, K.ORDER, K.LIMIT, K.KEY_LOOKUP, K.VECTOR_SEARCH):
            self.assertEqual(view[kind].level, S.SOURCE, kind)
        self.assertEqual(view[K.COMBINE].level, S.COORDINATOR)
        self.assertEqual(view[K.SEMANTIC_FILTER].level, S.COORDINATOR)
        self.assertEqual(view[K.SEMANTIC_FILTER].strategies, ("verify_all", "vector_shortlist"))
        self.assertEqual(view[K.COMBINE].strategies, ("read_order", "restrict_by_ids"))

    def test_planned_operators_are_unavailable_everywhere(self) -> None:
        view = by_kind(POSTGRES_CAPABILITIES)
        for kind in (K.JOIN, K.AGGREGATE, K.GROUP_BY, K.DISTINCT, K.UNION, K.TRAVERSE):
            self.assertEqual((view[kind].level, view[kind].strategies), (S.UNAVAILABLE, ()))

    def test_strategies_offered_never_exceed_the_catalog(self) -> None:
        for caps in (POSTGRES_CAPABILITIES, LIMITED):
            for item in operator_support(caps):
                self.assertTrue(set(item.strategies) <= set(OPERATORS.get(item.kind).strategies), item)

    def test_the_description_names_each_operator_once_with_its_strategies(self) -> None:
        text = describe_capabilities(POSTGRES_CAPABILITIES)
        for spec in OPERATORS:
            self.assertEqual(sum(line.split()[:1] == [spec.kind.value] for line in text.splitlines()), 1, spec.kind)
        self.assertIn("vector_shortlist", text)


class WeakAdapterViewTests(unittest.TestCase):
    """The declaration is what drives the view: a weaker source shows weaker support."""

    def test_a_source_that_cannot_order_limit_look_up_or_rank_hands_those_to_yodb(self) -> None:
        view = by_kind(LIMITED)
        self.assertEqual((view[K.ORDER].level, view[K.ORDER].strategies), (S.COORDINATOR, ("run_in_yodb",)))
        self.assertEqual((view[K.LIMIT].level, view[K.LIMIT].strategies), (S.COORDINATOR, ("run_in_yodb",)))
        self.assertEqual((view[K.KEY_LOOKUP].level, view[K.KEY_LOOKUP].strategies), (S.UNAVAILABLE, ()))
        self.assertEqual(view[K.VECTOR_SEARCH].level, S.UNAVAILABLE)

    def test_without_key_lookup_or_vector_search_the_coordinator_operators_lose_those_strategies(self) -> None:
        view = by_kind(LIMITED)
        self.assertEqual(view[K.COMBINE].strategies, ("read_order",))
        self.assertEqual(view[K.SEMANTIC_FILTER].strategies, ("verify_all",))

    def test_a_partial_filter_is_still_a_source_operator_but_none_means_yodb_filters(self) -> None:
        self.assertEqual(by_kind(LIMITED)[K.FILTER].level, S.SOURCE)
        none = replace(LIMITED, filter_operators=frozenset())
        self.assertEqual((by_kind(none)[K.FILTER].level, by_kind(none)[K.FILTER].strategies), (S.COORDINATOR, ("run_in_yodb",)))

    def test_a_vector_capable_source_gains_the_shortlist_strategy(self) -> None:
        from yodb.catalog import VectorMetric
        from yodb.planning import VectorSearchCapability

        caps = replace(LIMITED, vector_search=VectorSearchCapability(frozenset({VectorMetric.L2}), combines_with_filters=False, maximum_shortlist=50))
        view = by_kind(caps)
        self.assertEqual(view[K.VECTOR_SEARCH].level, S.SOURCE)
        self.assertIn("shortlist up to 50", view[K.VECTOR_SEARCH].detail)
        self.assertIn("vector_shortlist", view[K.SEMANTIC_FILTER].strategies)


CONTAINS = {"field": "subject", "op": "contains", "value": "x"}


def every_node_type_plan():
    """One plan containing every node type: two scans, assembly, residual filter, semantic, sort, project."""

    return ticket_plan(
        q({"all": [OWNER, CONTAINS, sem()]}, select=("subject", "owner")),
        semantic=SemanticPolicy(embedder=EMBEDDER_INFO, embedder_dimensions=3),
    )


def nodes_of(plan):
    yield plan
    for child in plan.inputs():
        yield from nodes_of(child)


class PlanNodeTests(unittest.TestCase):
    def test_every_node_names_an_implemented_catalog_operator(self) -> None:
        planned = every_node_type_plan()
        seen = {type(node) for node in nodes_of(planned.plan)}
        self.assertEqual(seen, {RemoteScan, RecordAssembly, CoordinatorFilter, SemanticVerify, CoordinatorSortPage, ResultProject})
        for node_type in seen:
            self.assertTrue(OPERATORS.get(node_type.operator).implemented, node_type)

    def test_each_node_type_names_a_different_operator(self) -> None:
        kinds = [t.operator for t in (RemoteScan, RecordAssembly, CoordinatorFilter, SemanticVerify, CoordinatorSortPage, ResultProject)]
        self.assertEqual(len(set(kinds)), len(kinds))
        self.assertEqual(kinds, [K.SCAN, K.COMBINE, K.FILTER, K.SEMANTIC_FILTER, K.ORDER, K.PROJECT])

    def test_inputs_and_with_inputs_round_trip_for_every_node(self) -> None:
        for node in nodes_of(every_node_type_plan().plan):
            self.assertEqual(node.with_inputs(node.inputs()), node, type(node).__name__)

    def test_unary_nodes_have_one_input_and_scans_none(self) -> None:
        for node in nodes_of(every_node_type_plan().plan):
            if isinstance(node, UnaryNode):
                self.assertEqual(len(node.inputs()), 1)
            if isinstance(node, RemoteScan):
                self.assertEqual(node.inputs(), ())

    def test_a_leaf_cannot_be_given_inputs(self) -> None:
        scan = next(n for n in nodes_of(every_node_type_plan().plan) if isinstance(n, RemoteScan))
        with self.assertRaises(ValueError):
            scan.with_inputs((scan,))

    def test_the_explanation_lists_inputs_before_the_node_that_consumes_them(self) -> None:
        planned = every_node_type_plan()
        kinds = [entry.kind for entry in explain_plan(planned.plan)]
        self.assertEqual(
            kinds,
            ["remote_scan", "remote_scan", "record_assembly", "coordinator_filter", "semantic_verify", "coordinator_sort_page", "result_project"],
        )
        self.assertEqual(tuple(explain_plan(planned.plan)), planned.explain.nodes)

    def test_the_fingerprint_comes_from_the_nodes_and_ignores_values(self) -> None:
        planned = every_node_type_plan()
        self.assertEqual(plan_fingerprint(planned.plan), planned.plan_fingerprint)
        other = ticket_plan(
            q({"all": [{**OWNER, "value": "someone-else"}, {**CONTAINS, "value": "y"}, sem("another proposition")]}, select=("subject", "owner")),
            semantic=SemanticPolicy(embedder=EMBEDDER_INFO, embedder_dimensions=3),
        )
        self.assertEqual(other.plan_fingerprint, planned.plan_fingerprint)

    def test_rewriting_with_an_identity_function_returns_an_equal_plan(self) -> None:
        planned = every_node_type_plan()
        self.assertEqual(transform_plan(planned.plan, lambda node: node), planned.plan)

    def test_a_rewrite_reaches_every_node_exactly_once_children_first(self) -> None:
        planned = every_node_type_plan()
        visited = []

        def record(node):
            visited.append(type(node))
            return node

        transform_plan(planned.plan, record)
        self.assertEqual(len(visited), len(list(nodes_of(planned.plan))))
        self.assertIs(visited[-1], ResultProject)   # the root last
        self.assertIs(visited[0], RemoteScan)       # a leaf first

    def test_a_rewrite_can_change_a_scan_deep_in_the_plan(self) -> None:
        from dataclasses import replace

        planned = every_node_type_plan()
        rewritten = transform_plan(planned.plan, lambda n: replace(n, maximum_rows=7) if isinstance(n, RemoteScan) else n)
        self.assertEqual({n.maximum_rows for n in nodes_of(rewritten) if isinstance(n, RemoteScan)}, {7})
        self.assertNotEqual(rewritten, planned.plan)

    def test_the_base_class_demands_the_description_methods(self) -> None:
        class Bare(PhysicalNode):
            operator = K.SCAN

        for call in (lambda: Bare().shape(), lambda: Bare().describe()):
            with self.assertRaises(NotImplementedError):
                call()


def executor_with(handlers=None):
    rows = SourceRowsExecutor({"helpdesk": HELPDESK, "directory": DIRECTORY})
    return FederatedPlanExecutor(
        QueryCompilerRegistry([PostgresQueryCompiler()]), QueryExecutionAdapterRegistry([rows]), handlers=handlers
    )


class ExecutionHandlerTests(unittest.TestCase):
    def test_every_core_node_type_has_a_default_handler(self) -> None:
        self.assertEqual(
            set(default_handlers()),
            {RemoteScan, RecordAssembly, CoordinatorFilter, CoordinatorSortPage, ResultProject},
        )

    def test_an_extension_contributes_the_handlers_for_the_nodes_it_adds(self) -> None:
        self.assertEqual(set(SemanticExtension().handlers()), {SemanticVerify})

    def test_a_node_without_a_handler_is_refused_not_silently_skipped(self) -> None:
        @dataclass(frozen=True)
        class Mystery(UnaryNode):
            input: object
            operator = K.DISTINCT

        planned = ticket_plan(q(OWNER, select=("subject", "owner")))
        with self.assertRaises(AssertionError) as caught:
            executor_with().execute(Mystery(input=planned.plan))
        self.assertIn("Mystery", str(caught.exception))

    def test_a_brand_new_operator_is_added_with_a_node_and_a_handler_only(self) -> None:
        """No planner, optimizer or executor change: a node type plus a handler."""

        @dataclass(frozen=True)
        class TakeFirst(UnaryNode):
            input: object
            count: int
            operator = K.LIMIT

            def shape(self):
                return {"kind": "take_first", "input": self.input.shape(), "count": self.count}

            def describe(self):
                return PlanExplanationNode("take_first", "coordinator", (), limit=self.count)

        def take_first(ctx: ExecutionContext, node: TakeFirst, run: Run):
            return ctx.execute(node.input, run)[: node.count]

        planned = ticket_plan(q({"field": "priority", "op": "gte", "value": 1}, select=("subject",)))
        plan = TakeFirst(input=planned.plan, count=2)
        rows = executor_with({TakeFirst: take_first}).execute(plan)
        self.assertEqual(len(rows), 2)
        self.assertEqual([entry.kind for entry in explain_plan(plan)][-1], "take_first")   # explain needs no change
        self.assertNotEqual(plan_fingerprint(plan), planned.plan_fingerprint)             # nor does the fingerprint

    def test_a_handler_can_be_overridden_for_testing_or_an_alternative_implementation(self) -> None:
        calls = []

        def counting_project(ctx, node, run):
            calls.append(type(node).__name__)
            return default_handlers()[ResultProject](ctx, node, run)

        planned = ticket_plan(q({"field": "priority", "op": "gte", "value": 1}, select=("subject",)))
        rows = executor_with({ResultProject: counting_project}).execute(planned.plan)
        self.assertEqual(calls, ["ResultProject"])
        self.assertTrue(rows)


if __name__ == "__main__":
    unittest.main()
