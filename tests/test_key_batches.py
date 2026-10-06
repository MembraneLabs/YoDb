"""A set of learned IDs larger than one restriction may carry is sent in several restricted reads."""

from __future__ import annotations

import unittest

from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.errors import ErrorCode, QueryExecutionError
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine
from yodb.planning import FederatedPhysicalPlanner, PlannerPolicy, PostgresPlanningAdapter, SourcePlanningRegistry

from support.catalogs import StaticRuntime
from support.shop import shop_active
from support.sql_world import SqlWorld

CUSTOMERS = [(f"c{i:04d}", f"N{i}", "US", None, i) for i in range(1, 2501)]                  # 2,500 customers
PLANS = [(f"c{i:04d}", "pro" if i <= 1800 else "basic") for i in range(1, 2501)]             # 1,800 are "pro"


def world() -> SqlWorld:
    return SqlWorld({
        "crm.customers": (("customer_id", "name", "country", "referrer_id", "seats"), CUSTOMERS),
        "billing.plans": (("customer_id", "plan"), PLANS),
        "desk.tickets": (("ticket_id", "customer_id", "subject", "status", "priority", "legacy_key", "body"), []),
        "triage.assignments": (("ticket_id", "assignee"), []),
    })


def engine(sql_world, **policy):
    planner = FederatedPhysicalPlanner(SourcePlanningRegistry([PostgresPlanningAdapter()]), policy=PlannerPolicy(**policy))
    return QueryExecutionEngine(
        StaticRuntime(shop_active()),
        QueryCompilerRegistry([PostgresQueryCompiler()]),
        QueryExecutionAdapterRegistry([sql_world]),
        planner=planner,
    )


PRO = {"from": {"dataset": "customer"}, "select": ["name", "plan"], "where": {"field": "plan", "op": "eq", "value": "pro"}, "page": {"first": 500}}
ALL = {"from": {"dataset": "customer"}, "select": ["name", "plan"], "page": {"first": 500}}
EXPECTED_PRO = [{"id": f"c{i:04d}", "name": f"N{i}", "plan": "pro"} for i in range(1, 501)]


def restricted(reads):
    return [r for r in reads if " IN (" in r.sql]


def key_counts(reads):
    return [r.sql.count("%s") - 1 for r in restricted(reads)]        # the parameters of the IN list, less the row limit


class KeyBatchTests(unittest.TestCase):
    def test_an_id_set_over_the_bound_is_sent_in_batches_and_the_answer_is_the_same(self) -> None:
        w = world()
        rows = [dict(r) for r in engine(w, maximum_transfer_keys=1000).execute(PRO).rows]
        self.assertEqual(rows, EXPECTED_PRO)
        reads = w.reads_of("crm")
        self.assertEqual(key_counts(reads), [1000, 800])                 # 1,800 IDs: two restricted reads, none unrestricted
        self.assertEqual(len(reads), 2)

    def test_the_answer_does_not_depend_on_the_batch_size(self) -> None:
        for bound in (100, 700, 1000, 5000):
            with self.subTest(bound=bound):
                got = [dict(r) for r in engine(world(), maximum_transfer_keys=bound, maximum_key_batches=100).execute(PRO).rows]
                self.assertEqual(got, EXPECTED_PRO)

    def test_batches_are_capped_and_beyond_the_cap_the_read_is_a_plain_scan(self) -> None:
        w = world()
        rows = [dict(r) for r in engine(w, maximum_transfer_keys=500, maximum_key_batches=3).execute(PRO).rows]   # 1,800 > 3 x 500
        self.assertEqual(rows, EXPECTED_PRO)
        self.assertEqual(restricted(w.reads_of("crm")), [])              # fell back to one unrestricted scan
        self.assertEqual(len(w.reads_of("crm")), 1)

    def test_one_batch_means_the_old_behaviour(self) -> None:
        w = world()
        rows = [dict(r) for r in engine(w, maximum_transfer_keys=1000, maximum_key_batches=1).execute(PRO).rows]
        self.assertEqual(rows, EXPECTED_PRO)
        self.assertEqual(restricted(w.reads_of("crm")), [])

    def test_enriching_many_rows_from_an_optional_source_is_batched_too(self) -> None:
        w = world()
        rows = [dict(r) for r in engine(w, maximum_transfer_keys=1000).execute(ALL).rows]
        self.assertEqual(rows[0], {"id": "c0001", "name": "N1", "plan": "pro"})
        self.assertEqual(len(rows), 500)
        # 2,500 customers are read (the anchor), and then their plans in three restricted reads
        self.assertEqual(key_counts(w.reads_of("billing")), [1000, 1000, 500])

    def test_the_row_guard_counts_all_the_batches_together(self) -> None:
        with self.assertRaises(QueryExecutionError) as caught:
            engine(world(), maximum_transfer_keys=1000, maximum_rows_per_source=1500).execute(PRO)     # 1,800 rows in total
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_ROW_LIMIT_EXCEEDED)

    def test_a_policy_needs_at_least_one_batch(self) -> None:
        with self.assertRaises(ValueError):
            PlannerPolicy(maximum_key_batches=0)

    def test_the_time_budget_is_checked_between_batches(self) -> None:
        with self.assertRaises(QueryExecutionError) as caught:
            engine(world(), maximum_transfer_keys=10, maximum_key_batches=500).execute(PRO, timeout_seconds=1e-9)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_TIMEOUT)


if __name__ == "__main__":
    unittest.main()
