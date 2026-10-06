"""Joining two datasets over a declared relationship.

Every result is compared with a join computed in plain Python over the same data; the sources are
in-memory SQL (``SqlWorld``), so each side really is filtered by its predicates and restricted by
its batch of keys.
"""

from __future__ import annotations

from functools import cmp_to_key
import random
import unittest

from yodb.catalog import CatalogValidationError, SourceKind
from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.errors import ErrorCode, QueryError, QueryExecutionError
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine
from yodb.planning import (
    FederatedPhysicalPlanner,
    JoinPolicy,
    PostgresPlanningAdapter,
    SourcePlanningRegistry,
    SourceStatistics,
    StatisticsService,
)
from yodb.semantic import SemanticExtension, SemanticPolicy, SemanticRuntime

from support.catalogs import StaticRuntime
from support.shop import DATASETS, RELATIONS, SOURCES, customer_rows, shop_active, shop_world, ticket_rows
from support.sql_world import evaluation
from support.statistics import MapProvider
from support.tickets import KeywordVerifier

ACTIVE = shop_active()


def engine(world, *, statistics=None, join_policy=JoinPolicy(), semantic=None):
    extensions = () if semantic is None else (semantic,)
    planner = FederatedPhysicalPlanner(
        SourcePlanningRegistry([PostgresPlanningAdapter()]), statistics=statistics, extensions=extensions
    )
    return QueryExecutionEngine(
        StaticRuntime(ACTIVE),
        QueryCompilerRegistry([PostgresQueryCompiler()]),
        QueryExecutionAdapterRegistry([world]),
        planner=planner,
        statistics=statistics,
        extensions=extensions,
        join_policy=join_policy,
    )


def counts(customers: int, tickets: int) -> StatisticsService:
    """Statistics that make one side look much bigger than the other."""

    provider = MapProvider({
        "crm.customers": SourceStatistics(row_count=customers), "billing.plans": SourceStatistics(row_count=customers),
        "desk.tickets": SourceStatistics(row_count=tickets), "triage.assignments": SourceStatistics(row_count=tickets),
    })
    return StatisticsService({SourceKind.POSTGRES: provider})


def p(field, op, value=None):
    leaf = {"field": field, "op": op}
    if op not in ("is_null", "is_not_null"):
        leaf["value"] = value
    return leaf


def traverse(*, relationship="customer_has_ticket", alias="ticket", select=None, where=None, optional=False, direction=None):
    step = {"relationship": relationship, "as": alias}
    for key, value in (("select", select), ("where", where), ("direction", direction)):
        if value is not None:
            step[key] = value
    if optional:
        step["optional"] = True
    return [step]


def query(select=("name", "country"), where=None, steps=None, order_by=None, first=100, dataset="customer", **extra):
    raw = {"from": {"dataset": dataset}, "select": list(select), "page": {"first": first}, "traverse": steps if steps is not None else traverse()}
    if where is not None:
        raw["where"] = where
    if order_by is not None:
        raw["order_by"] = order_by
    return {**raw, **extra}


# --- the oracle: a join in plain Python --------------------------------------------------------------------------------


def _compare(a, b, descending):
    if a is None and b is None:
        return 0
    if a is None:
        return -1 if descending else 1          # NULLs last for ascending, first for descending
    if b is None:
        return 1 if descending else -1
    if a == b:
        return 0
    result = -1 if a < b else 1
    return -result if descending else result


def oracle(left_rows, right_rows, left_key, right_key, *, left_ok=lambda r: True, right_ok=lambda r: True, optional=False,
           alias="ticket", left_columns, right_columns, order=(), first=100):
    pairs = []
    for left in left_rows:
        if not left_ok(left):
            continue
        matches = [r for r in right_rows if right_ok(r) and left[left_key] is not None and r[right_key] == left[left_key]]
        if not matches and optional:
            pairs.append((left, None))
        pairs.extend((left, m) for m in matches)

    def value(pair, column):
        left, right = pair
        if column.startswith(alias + "."):
            return None if right is None else right.get(column[len(alias) + 1:])
        return left.get(column)

    terms = [*order, ("id", False), (f"{alias}.id", False)]

    def cmp(a, b):
        for column, descending in terms:
            result = _compare(value(a, column), value(b, column), descending)
            if result:
                return result
        return 0

    pairs.sort(key=cmp_to_key(cmp))
    columns = [*left_columns, *(f"{alias}.{c}" for c in right_columns)]
    return [{c: value(pair, c) for c in columns} for pair in pairs[:first]]


def rows_of(result):
    return [dict(row) for row in result.rows]


class JoinResultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = shop_world()
        self.db = engine(self.world)
        self.customers, self.tickets = customer_rows(), ticket_rows()

    def run_join(self, **kw):
        return rows_of(self.db.execute(query(**kw)))

    def test_an_inner_join_matches_every_customer_with_its_tickets(self) -> None:
        got = self.run_join(steps=traverse(select=["subject", "status"]), where=p("country", "eq", "US"))
        expected = oracle(
            self.customers, self.tickets, "id", "customer_id", left_ok=lambda r: r["country"] == "US",
            left_columns=["id", "name", "country"], right_columns=["id", "subject", "status"],
        )
        self.assertEqual(got, expected)
        self.assertEqual(got[0], {"id": "c1", "name": "Ann", "country": "US", "ticket.id": "t1", "ticket.subject": "Login", "ticket.status": "open"})
        self.assertEqual(list(got[0]), ["id", "name", "country", "ticket.id", "ticket.subject", "ticket.status"])   # id first, then the selection

    def test_one_customer_with_several_tickets_appears_once_per_ticket(self) -> None:
        got = self.run_join(steps=traverse(select=["subject"]), where=p("name", "eq", "Ann"))
        self.assertEqual([r["ticket.id"] for r in got], ["t1", "t13", "t2", "t3"])
        self.assertEqual({r["id"] for r in got}, {"c1"})

    def test_customers_without_a_match_and_tickets_without_a_customer_are_dropped_by_an_inner_join(self) -> None:
        got = self.run_join(steps=traverse(select=["subject"]), select=("name",))
        self.assertNotIn("c7", {r["id"] for r in got})                 # no tickets
        self.assertNotIn("t11", {r["ticket.id"] for r in got})         # no customer
        self.assertNotIn("t12", {r["ticket.id"] for r in got})         # a customer that does not exist

    def test_filters_on_the_traversed_side_restrict_the_matches(self) -> None:
        got = self.run_join(steps=traverse(select=["subject"], where={"all": [p("status", "eq", "open"), p("priority", "gte", 4)]}))
        self.assertEqual([(r["id"], r["ticket.id"]) for r in got], [("c1", "t3"), ("c3", "t6"), ("c5", "t8")])

    def test_a_left_join_keeps_customers_with_no_match_and_their_right_fields_are_null(self) -> None:
        got = self.run_join(steps=traverse(select=["subject"], optional=True), select=("name",), where=p("country", "eq", "UK"))
        expected = oracle(
            self.customers, self.tickets, "id", "customer_id", left_ok=lambda r: r["country"] == "UK", optional=True,
            left_columns=["id", "name"], right_columns=["id", "subject"],
        )
        self.assertEqual(got, expected)
        gus = [r for r in got if r["id"] == "c7"]
        self.assertEqual(gus, [{"id": "c7", "name": "Gus", "ticket.id": None, "ticket.subject": None}])

    def test_in_a_left_join_a_filter_on_the_traversed_side_only_removes_matches_not_customers(self) -> None:
        got = self.run_join(steps=traverse(select=["status"], where=p("status", "eq", "pending"), optional=True), select=("name",))
        self.assertEqual({r["id"] for r in got}, {c["id"] for c in self.customers})
        self.assertEqual([r["id"] for r in got if r["ticket.id"] is not None], ["c1", "c4"])

    def test_filters_and_fields_of_a_side_that_spans_two_sources_work(self) -> None:
        got = self.run_join(
            steps=traverse(select=["subject", "assignee"], where=p("assignee", "eq", "ann")), select=("name", "plan"), where=p("plan", "eq", "pro")
        )
        expected = oracle(
            self.customers, self.tickets, "id", "customer_id", left_ok=lambda r: r["plan"] == "pro", right_ok=lambda r: r["assignee"] == "ann",
            left_columns=["id", "name", "plan"], right_columns=["id", "subject", "assignee"],
        )
        self.assertEqual(got, expected)
        self.assertEqual({r["ticket.assignee"] for r in got}, {"ann"})

    def test_ordering_by_fields_of_both_sides_with_nulls_where_the_standard_puts_them(self) -> None:
        order = [{"field": "ticket.priority", "direction": "desc"}, {"field": "name", "direction": "asc"}]
        got = self.run_join(steps=traverse(select=["priority"], optional=True), select=("name",), order_by=order)
        expected = oracle(
            self.customers, self.tickets, "id", "customer_id", optional=True, left_columns=["id", "name"], right_columns=["id", "priority"],
            order=[("ticket.priority", True), ("name", False)],
        )
        self.assertEqual(got, expected)
        self.assertEqual([r["ticket.priority"] for r in got][:3], [None, None, 5])    # descending puts NULL first: Gus and Hal have no tickets

    def test_ordering_by_a_field_that_is_not_selected_still_orders_and_does_not_appear(self) -> None:
        order = [{"field": "ticket.priority", "direction": "asc"}]
        got = self.run_join(steps=traverse(select=["subject"]), select=("name",), order_by=order)
        self.assertNotIn("ticket.priority", got[0])
        priorities = [t["priority"] for t in sorted(self.tickets, key=lambda t: (t["priority"], t["id"])) if t["customer_id"] in {c["id"] for c in self.customers}]
        self.assertEqual([r["ticket.id"] for r in got], [t["id"] for t in sorted(
            (t for t in self.tickets if t["customer_id"] in {c["id"] for c in self.customers}), key=lambda t: (t["priority"], t["customer_id"], t["id"]))])
        self.assertEqual(len(priorities), len(got))

    def test_the_page_is_taken_from_the_joined_rows(self) -> None:
        order = [{"field": "ticket.priority", "direction": "desc"}]
        full = self.run_join(steps=traverse(select=["priority"]), select=("name",), order_by=order)
        self.assertEqual(self.run_join(steps=traverse(select=["priority"]), select=("name",), order_by=order, first=4), full[:4])
        capped = self.run_join(steps=traverse(select=["priority"]), select=("name",), order_by=order, first=10, constraints={"maximum_results": 3})
        self.assertEqual(capped, full[:3])

    def test_without_a_select_every_public_field_of_both_sides_comes_back_but_not_internal_ones(self) -> None:
        raw = query(steps=traverse(), where=p("name", "eq", "Ann"), first=1)
        del raw["select"]
        row = rows_of(self.db.execute(raw))[0]
        self.assertIn("ticket.subject", row)
        self.assertIn("plan", row)
        self.assertNotIn("ticket.legacy_key", row)

    def test_a_relationship_can_be_named_by_its_alias(self) -> None:
        by_alias = self.run_join(steps=traverse(relationship="tickets", select=["subject"]), where=p("name", "eq", "Cai"))
        self.assertEqual([r["ticket.id"] for r in by_alias], ["t5", "t6"])

    def test_a_bidirectional_relationship_can_be_walked_from_the_other_end(self) -> None:
        raw = {"from": {"dataset": "ticket"}, "select": ["subject"], "where": p("status", "eq", "pending"), "page": {"first": 20},
               "traverse": traverse(relationship="customer_ticket_both_ways", alias="owner", select=["name", "country"], direction="reverse")}
        got = rows_of(self.db.execute(raw))
        self.assertEqual([(r["id"], r["owner.id"], r["owner.name"]) for r in got], [("t13", "c1", "Ann"), ("t7", "c4", "Dee")])

    def test_a_dataset_can_be_joined_to_itself(self) -> None:
        raw = query(select=("name",), steps=traverse(relationship="referred_by", alias="referrer", select=["name"]), where=p("country", "eq", "US"))
        got = rows_of(self.db.execute(raw))
        self.assertEqual([(r["name"], r["referrer.name"]) for r in got], [("Cai", "Ann"), ("Fay", "Cai")])
        raw = query(select=("name",), steps=traverse(relationship="referred_by", alias="referrer", select=["name"], optional=True), where=p("country", "eq", "US"))
        self.assertEqual([(r["name"], r["referrer.name"]) for r in rows_of(self.db.execute(raw))], [("Ann", None), ("Cai", "Ann"), ("Fay", "Cai")])

    def test_a_semantic_condition_on_either_side_works_inside_a_join(self) -> None:
        extension = SemanticExtension(SemanticRuntime(KeywordVerifier(), None, 3), policy=SemanticPolicy())
        db = engine(self.world, semantic=extension, join_policy=JoinPolicy(batch_size=3))
        raw = query(select=("name",), steps=traverse(select=["subject"], where={"semantic": {"field": "body", "proposition": "mentions refund"}}))
        got = rows_of(db.execute(raw))
        self.assertEqual([r["ticket.id"] for r in got], ["t13", "t2", "t14", "t6"])
        self.assertEqual(sorted(r["id"] for r in got), ["c1", "c1", "c2", "c3"])

    def test_the_result_carries_a_stable_fingerprint(self) -> None:
        first = self.db.execute(query(where=p("country", "eq", "US")))
        second = self.db.execute(query(where=p("country", "eq", "UK")))
        self.assertEqual(len(first.query_fingerprint), 64)
        self.assertEqual(first.query_fingerprint, self.db.execute(query(where=p("country", "eq", "US"))).query_fingerprint)
        self.assertNotEqual(first.query_fingerprint, second.query_fingerprint)                  # a different question
        self.assertNotEqual(first.query_fingerprint, self.db.execute(query(where=p("country", "eq", "US"), steps=traverse(optional=True))).query_fingerprint)


class DrivingSideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = shop_world()

    def explain(self, raw, **kw):
        return engine(self.world, **kw).explain(raw)

    def test_without_statistics_the_side_with_more_filters_drives(self) -> None:
        right_filtered = query(steps=traverse(where={"all": [p("status", "eq", "open"), p("priority", "gte", 4)]}), where=p("country", "eq", "US"))
        notes = self.explain(right_filtered).optimizer
        self.assertIn("driver=right", notes)
        self.assertEqual(self.explain(query(where={"all": [p("country", "eq", "US"), p("seats", "gt", 1)]}, steps=traverse(where=p("status", "eq", "open")))).optimizer[1], "driver=left")

    def test_with_statistics_the_side_expected_to_produce_fewer_rows_drives(self) -> None:
        raw = query(where=p("country", "eq", "US"), steps=traverse(where=p("status", "eq", "open")))
        small_tickets = self.explain(raw, statistics=counts(customers=1_000_000, tickets=100)).optimizer
        small_customers = self.explain(raw, statistics=counts(customers=100, tickets=1_000_000)).optimizer
        self.assertEqual((small_tickets[0], small_tickets[1]), ("strategy=cost_based", "driver=right"))
        self.assertEqual((small_customers[0], small_customers[1]), ("strategy=cost_based", "driver=left"))

    def test_a_left_join_is_always_driven_from_the_root(self) -> None:
        raw = query(steps=traverse(where={"all": [p("status", "eq", "open"), p("priority", "gte", 4)]}, optional=True))
        self.assertEqual(self.explain(raw, statistics=counts(1_000_000, 10)).optimizer[1], "driver=left")

    def test_the_result_does_not_depend_on_which_side_drives(self) -> None:
        raw = query(select=("name",), steps=traverse(select=["subject"], where=p("status", "eq", "open")), where=p("country", "eq", "US"))
        by_left = rows_of(engine(self.world, statistics=counts(10, 1_000_000)).execute(raw))
        by_right = rows_of(engine(self.world, statistics=counts(1_000_000, 10)).execute(raw))
        self.assertEqual(by_left, by_right)
        self.assertTrue(by_left)

    def test_the_driving_side_is_read_first_and_the_probing_side_only_for_its_keys(self) -> None:
        world = shop_world()
        db = engine(world, statistics=counts(1_000_000, 100))
        db.execute(query(select=("name",), where=p("country", "eq", "US"), steps=traverse(where=p("status", "eq", "open"))))
        self.assertEqual([q.source_name for q in world.queries][:1], ["desk"])                     # tickets first
        crm = world.reads_of("crm")
        self.assertEqual(len(crm), 1)
        self.assertIn('"customer_id" IN (', crm[0].sql)                                              # customers only for those keys


class BatchingAndGuardTests(unittest.TestCase):
    def test_keys_go_to_the_other_side_in_batches_and_the_result_is_the_same(self) -> None:
        world = shop_world()
        raw = query(select=("name",), steps=traverse(select=["subject"]))
        batched = rows_of(engine(world, join_policy=JoinPolicy(batch_size=3)).execute(raw))
        reads = world.reads_of("desk")
        self.assertEqual(len(reads), 3)                                    # 8 customers, 3 at a time
        for read in reads:
            self.assertLessEqual(read.sql.count("%s"), 3 + 1)               # a batch of keys and the row limit
        self.assertEqual(batched, rows_of(engine(shop_world()).execute(raw)))

    def test_a_driving_side_over_its_limit_is_refused_with_advice_not_truncated(self) -> None:
        with self.assertRaises(QueryExecutionError) as caught:
            engine(shop_world(), join_policy=JoinPolicy(maximum_driver_rows=5)).execute(query())
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_ROW_LIMIT_EXCEEDED)
        self.assertIn("add a filter", caught.exception.detail.message)

    def test_matches_over_the_probe_limit_are_refused(self) -> None:
        with self.assertRaises(QueryExecutionError) as caught:
            engine(shop_world(), join_policy=JoinPolicy(maximum_probe_rows=4)).execute(query())
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_COORDINATOR_LIMIT_EXCEEDED)

    def test_a_join_larger_than_the_joined_row_limit_is_refused(self) -> None:
        with self.assertRaises(QueryExecutionError) as caught:
            engine(shop_world(), join_policy=JoinPolicy(maximum_joined_rows=5)).execute(query())
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_COORDINATOR_LIMIT_EXCEEDED)

    def test_the_whole_join_shares_one_time_budget(self) -> None:
        with self.assertRaises(QueryExecutionError) as caught:
            engine(shop_world()).execute(query(), timeout_seconds=1e-9)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_TIMEOUT)

    def test_invalid_policies_are_rejected(self) -> None:
        for kwargs in ({"batch_size": 0}, {"maximum_driver_rows": 0}, {"maximum_probe_rows": 0}, {"maximum_joined_rows": 0}):
            with self.assertRaises(ValueError):
                JoinPolicy(**kwargs)


class ExplainTests(unittest.TestCase):
    def test_explain_shows_the_join_and_both_sides_without_running_anything(self) -> None:
        world = shop_world()
        explanation = engine(world).explain(query(where=p("country", "eq", "US"), steps=traverse(where=p("status", "eq", "open"))))
        self.assertEqual(explanation.plan_kind, "hash_join")
        kinds = [node.kind for node in explanation.nodes]
        self.assertEqual(kinds.count("hash_join"), 1)
        self.assertGreaterEqual(kinds.count("remote_scan"), 2)
        join = next(node for node in explanation.nodes if node.kind == "hash_join")
        self.assertIn("on customer.id = ticket.customer_id", join.detail)
        self.assertEqual(world.queries, [])

    def test_the_plan_fingerprint_ignores_values_but_not_structure(self) -> None:
        db = engine(shop_world())
        us = db.explain(query(where=p("country", "eq", "US"))).plan_fingerprint
        uk = db.explain(query(where=p("country", "eq", "UK"))).plan_fingerprint
        self.assertEqual(us, uk)
        self.assertNotEqual(us, db.explain(query(where=p("country", "eq", "US"), steps=traverse(optional=True))).plan_fingerprint)


class ErrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = engine(shop_world())

    def refused(self, raw, code, *, location=None, contains=None):
        with self.assertRaises(QueryError) as caught:
            self.db.execute(raw)
        self.assertEqual(caught.exception.code, code)
        if location is not None:
            self.assertEqual(caught.exception.detail.location, location)
        if contains is not None:
            self.assertIn(contains, caught.exception.detail.message)

    def test_unknown_relationship(self) -> None:
        self.refused(query(steps=traverse(relationship="nope")), ErrorCode.RELATIONSHIP_NOT_FOUND, location="traverse[0].relationship")

    def test_a_relationship_that_does_not_start_at_the_root_dataset(self) -> None:
        self.refused(query(dataset="ticket", select=("subject",)), ErrorCode.RELATIONSHIP_NOT_APPLICABLE, contains="goes from 'customer'")
        raw = query(dataset="ticket", select=("subject",), steps=traverse(relationship="customer_ticket_both_ways"))
        self.refused(raw, ErrorCode.RELATIONSHIP_NOT_APPLICABLE, contains="reverse")      # a bidirectional one says how

    def test_a_one_way_relationship_cannot_be_reversed(self) -> None:
        raw = query(dataset="ticket", select=("subject",), steps=traverse(direction="reverse"))
        self.refused(raw, ErrorCode.RELATIONSHIP_NOT_APPLICABLE, location="traverse[0].direction")

    def test_joining_on_an_internal_field_is_refused(self) -> None:
        self.refused(query(steps=traverse(relationship="customer_legacy_link")), ErrorCode.FIELD_NOT_ACCESSIBLE, contains="internal")

    def test_errors_in_the_traversed_side_name_where_in_the_traversal(self) -> None:
        self.refused(query(steps=traverse(select=["nope"])), ErrorCode.FIELD_NOT_FOUND, location="traverse[0].select[0]")
        self.refused(query(steps=traverse(where=p("nope", "eq", 1))), ErrorCode.FIELD_NOT_FOUND)
        self.refused(query(steps=traverse(where=p("priority", "contains", "x"))), ErrorCode.QUERY_OPERATOR_NOT_SUPPORTED)

    def test_errors_in_the_root_side_are_reported_as_for_any_query(self) -> None:
        self.refused(query(select=("nope",)), ErrorCode.FIELD_NOT_FOUND, location="select[0]")

    def test_the_traversal_is_validated(self) -> None:
        for steps, code in (
            ([], ErrorCode.QUERY_SHAPE_INVALID),
            ("tickets", ErrorCode.QUERY_SHAPE_INVALID),
            ([{"as": "t"}], ErrorCode.QUERY_SHAPE_INVALID),
            ([{"relationship": "tickets", "as": "not valid!"}], ErrorCode.QUERY_SHAPE_INVALID),
            ([{"relationship": "tickets", "extra": 1}], ErrorCode.QUERY_SHAPE_INVALID),
            ([{"relationship": "tickets", "optional": "yes"}], ErrorCode.QUERY_SHAPE_INVALID),
            ([{"relationship": "tickets", "direction": "sideways"}], ErrorCode.QUERY_SHAPE_INVALID),
            ([{"relationship": "tickets", "select": ["id", "id"]}], ErrorCode.QUERY_SHAPE_INVALID),
            (traverse() + traverse(), ErrorCode.QUERY_FEATURE_NOT_SUPPORTED),
        ):
            with self.subTest(steps=steps):
                self.refused(query(steps=steps), code)

    def test_order_by_must_name_a_field_of_a_side(self) -> None:
        self.refused(query(order_by=[{"field": "other.priority", "direction": "asc"}]), ErrorCode.FIELD_NOT_FOUND)
        self.refused(query(order_by=[{"field": "ticket.nope", "direction": "asc"}]), ErrorCode.FIELD_NOT_FOUND)
        self.refused(query(order_by=[{"field": "nope", "direction": "asc"}]), ErrorCode.FIELD_NOT_FOUND)
        self.refused(query(order_by=[{"field": "ticket.legacy_key", "direction": "asc"}]), ErrorCode.FIELD_NOT_ACCESSIBLE)
        raw = query(steps=traverse(select=["subject"]), order_by=[{"field": "ticket.nope", "direction": "asc"}])
        self.refused(raw, ErrorCode.FIELD_NOT_FOUND)               # also when the traversal lists its fields

    def test_a_cursor_is_refused_and_an_oversized_page_too(self) -> None:
        self.refused({**query(), "page": {"first": 5, "after": "x"}}, ErrorCode.QUERY_FEATURE_NOT_SUPPORTED)
        self.refused(query(first=501), ErrorCode.QUERY_LIMIT_INVALID)

    def test_unknown_top_level_keys_are_still_refused(self) -> None:
        self.refused({**query(), "sql": "SELECT 1"}, ErrorCode.QUERY_SHAPE_INVALID)


class CatalogTests(unittest.TestCase):
    def test_a_relationship_that_compares_fields_of_different_types_is_rejected_when_the_catalog_loads(self) -> None:
        broken = RELATIONS.replace("to: {source: desk, field: customer_id}", "to: {source: desk, field: priority}", 1)
        with self.assertRaises(CatalogValidationError) as caught:
            evaluation(DATASETS, SOURCES, broken)
        self.assertIn("must have the same type", str(caught.exception))


class EquivalenceTests(unittest.TestCase):
    """Random joins, with either side driving, against the Python oracle."""

    LEFT = [
        (None, lambda r: True), (p("country", "eq", "US"), lambda r: r["country"] == "US"),
        (p("country", "is_null"), lambda r: r["country"] is None), (p("seats", "gte", 5), lambda r: r["seats"] >= 5),
        (p("plan", "eq", "pro"), lambda r: r["plan"] == "pro"), (p("plan", "is_null"), lambda r: r["plan"] is None),
        ({"all": [p("country", "ne", "DE"), p("seats", "lt", 10)]}, lambda r: r["country"] is not None and r["country"] != "DE" and r["seats"] < 10),
        ({"any": [p("name", "eq", "Ann"), p("plan", "eq", "basic")]}, lambda r: r["name"] == "Ann" or r["plan"] == "basic"),
    ]
    RIGHT = [
        (None, lambda r: True), (p("status", "eq", "open"), lambda r: r["status"] == "open"),
        (p("priority", "gte", 4), lambda r: r["priority"] >= 4), (p("assignee", "eq", "ann"), lambda r: r["assignee"] == "ann"),
        (p("assignee", "is_null"), lambda r: r["assignee"] is None),
        ({"all": [p("status", "ne", "closed"), p("priority", "lt", 5)]}, lambda r: r["status"] != "closed" and r["priority"] < 5),
        ({"any": [p("status", "eq", "pending"), p("priority", "eq", 1)]}, lambda r: r["status"] == "pending" or r["priority"] == 1),
    ]
    ORDERS = [
        [], [("name", False)], [("ticket.priority", True), ("name", False)], [("ticket.status", False), ("ticket.priority", True)],
        [("country", True), ("ticket.id", True)], [("seats", False), ("ticket.priority", False)],
    ]

    def test_random_joins_equal_the_oracle(self) -> None:
        rng = random.Random(20261005)
        customers, tickets = customer_rows(), ticket_rows()
        statistics = [None, counts(10, 1_000_000), counts(1_000_000, 10)]
        world = shop_world()
        engines = [engine(world, statistics=s, join_policy=JoinPolicy(batch_size=rng.choice([2, 3, 1000]))) for s in statistics]
        seen_right = seen_left = nonempty = 0
        for index in range(240):
            (left_where, left_ok), (right_where, right_ok) = rng.choice(self.LEFT), rng.choice(self.RIGHT)
            optional, order, first = rng.random() < 0.35, rng.choice(self.ORDERS), rng.choice([3, 10, 100])
            left_columns = ["id", "name", "country", "plan", "seats"]
            right_columns = ["id", "subject", "status", "priority", "assignee"]
            raw = query(
                select=("name", "country", "plan", "seats"), where=left_where, first=first, order_by=[
                    {"field": c, "direction": "desc" if d else "asc"} for c, d in order] or None,
                steps=traverse(select=["subject", "status", "priority", "assignee"], where=right_where, optional=optional),
            )
            expected = oracle(
                customers, tickets, "id", "customer_id", left_ok=left_ok, right_ok=right_ok, optional=optional,
                left_columns=left_columns, right_columns=right_columns, order=order, first=first,
            )
            nonempty += bool(expected)
            for db in engines:
                with self.subTest(case=index, statistics=statistics[engines.index(db)] is not None):
                    result = db.execute(raw)
                    self.assertEqual(rows_of(result), expected)
                    note = db.explain(raw).optimizer
                    seen_right += "driver=right" in note
                    seen_left += "driver=left" in note
        self.assertGreater(nonempty, 120)                     # not vacuous
        self.assertGreater(seen_right, 30)                    # both driving sides were really exercised
        self.assertGreater(seen_left, 30)


if __name__ == "__main__":
    unittest.main()
