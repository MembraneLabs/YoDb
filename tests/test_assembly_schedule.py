"""The executor follows the plan's schedule; an unsafe shortlist falls back safely."""

from __future__ import annotations

import unittest
from dataclasses import replace

from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.execution import ExecutionTrace, QueryExecutionAdapterRegistry
from yodb.execution.federated import FederatedPlanExecutor
from yodb.planning import AssemblyStep, default_schedule, RecordAssembly, StepRole
from yodb.semantic import SemanticExtension, SemanticPlanKind, SemanticPolicy, SemanticRuntime

from support.catalogs import SourceRowsExecutor
from support.tickets import (
    DIRECTORY,
    EMBEDDER_INFO,
    FakeEmbedder,
    HELPDESK,
    KeywordVerifier,
    OWNER,
    plan,
    prio,
    q,
    sem,
)


def assembly_of(planned) -> RecordAssembly:
    node = planned.plan
    while not isinstance(node, RecordAssembly):
        node = node.input
    return node


def with_assembly(node, **changes):
    if isinstance(node, RecordAssembly):
        return replace(node, **changes)
    return replace(node, input=with_assembly(node.input, **changes))


def run(planned, helpdesk=HELPDESK, directory=DIRECTORY, semantic=None, trace=None):
    executor = SourceRowsExecutor({"helpdesk": helpdesk, "directory": directory})
    runner = FederatedPlanExecutor(
        QueryCompilerRegistry([PostgresQueryCompiler()]), QueryExecutionAdapterRegistry([executor]), extensions=(SemanticExtension(semantic),)
    )
    rows = runner.execute(planned.plan, trace=trace)
    return rows, executor.queries


def names(queries):
    return [x.source_name for x in queries]


class DefaultScheduleTests(unittest.TestCase):
    def test_the_rule_schedule_reads_required_then_anchor_then_enrichers(self) -> None:
        planned = plan(q(OWNER, select=("subject", "owner")))
        steps = assembly_of(planned).schedule
        self.assertEqual([(s.source_name, s.role) for s in steps], [("directory", StepRole.REQUIRED), ("helpdesk", StepRole.ANCHOR)])
        optional = plan({"from": {"dataset": "ticket"}, "select": ["subject", "owner"], "where": prio(), "page": {"first": 5}})
        self.assertEqual(
            [(s.source_name, s.role) for s in assembly_of(optional).schedule],
            [("helpdesk", StepRole.ANCHOR), ("directory", StepRole.OPTIONAL)],
        )
        self.assertTrue(all(step.restrict for step in steps))

    def test_the_schedule_names_every_source_exactly_once(self) -> None:
        node = assembly_of(plan(q(OWNER, select=("subject", "owner"))))
        scans = (node.anchor, *node.contributors)
        self.assertEqual(sorted(s.source_name for s in node.schedule), sorted(s.source.source_name for s in scans))
        self.assertEqual(node.schedule, default_schedule(node.anchor, node.contributors, node.required_contributor_matches))


class ScheduleFollowingTests(unittest.TestCase):
    def planned(self):
        return plan(q({"all": [OWNER, prio()]}, select=("subject", "owner")))

    def test_the_default_order_restricts_the_anchor_by_the_required_ids(self) -> None:
        rows, queries = run(self.planned())
        self.assertEqual(names(queries), ["directory", "helpdesk"])
        self.assertIn('"id" IN (%s, %s, %s)', queries[1].sql)
        self.assertEqual([r["id"] for r in rows], ["t2", "t4", "t6"])

    def test_an_explicit_anchor_first_schedule_restricts_the_required_scan_by_the_anchor_ids(self) -> None:
        planned = self.planned()
        anchor_first = (AssemblyStep("helpdesk", StepRole.ANCHOR), AssemblyStep("directory", StepRole.REQUIRED))
        rows, queries = run(replace(planned, plan=with_assembly(planned.plan, schedule=anchor_first)))
        self.assertEqual(names(queries), ["helpdesk", "directory"])
        self.assertIn('"ticket_id" IN (%s, %s, %s, %s, %s, %s)', queries[1].sql)
        self.assertEqual([r["id"] for r in rows], ["t2", "t4", "t6"])

    def test_an_unrestricted_step_reads_plainly_even_when_ids_are_known(self) -> None:
        planned = self.planned()
        steps = (AssemblyStep("directory", StepRole.REQUIRED), AssemblyStep("helpdesk", StepRole.ANCHOR, restrict=False))
        rows, queries = run(replace(planned, plan=with_assembly(planned.plan, schedule=steps)))
        self.assertNotIn(" IN (", queries[1].sql)
        self.assertEqual([r["id"] for r in rows], ["t2", "t4", "t6"])  # the required match still applies

    def test_an_optional_read_placed_before_the_anchor_is_unrestricted_but_correct(self) -> None:
        planned = plan({"from": {"dataset": "ticket"}, "select": ["subject", "owner"], "where": prio(), "page": {"first": 10}})
        steps = (AssemblyStep("directory", StepRole.OPTIONAL), AssemblyStep("helpdesk", StepRole.ANCHOR))
        rows, queries = run(replace(planned, plan=with_assembly(planned.plan, schedule=steps)))
        self.assertEqual(names(queries), ["directory", "helpdesk"])
        self.assertNotIn(" IN (", queries[0].sql)
        self.assertEqual({r["id"]: r["owner"] for r in rows}["t2"], "ann")
        self.assertIsNone({r["id"]: r["owner"] for r in rows}["t1"])

    def test_an_empty_narrowing_step_ends_the_query_early(self) -> None:
        rows, queries = run(self.planned(), directory=())
        self.assertEqual(rows, ())
        self.assertEqual(names(queries), ["directory"])

    def test_keys_narrow_by_every_narrowing_step_and_optional_steps_use_the_intersection(self) -> None:
        planned = plan({"from": {"dataset": "ticket"}, "select": ["subject", "owner", "memo"], "where": {"all": [OWNER, prio()]}, "page": {"first": 10}})
        rows, queries = run(planned)
        self.assertEqual(rows[0]["id"], "t2")


class ShortlistSafetyTests(unittest.TestCase):
    def semantic_plan(self, **policy):
        raw = q({"all": [OWNER, sem()]}, select=("subject", "owner"))
        return plan(raw, semantic=SemanticPolicy(embedder=EMBEDDER_INFO, embedder_dimensions=3, **policy))

    def runtime(self):
        return SemanticRuntime(KeywordVerifier(), FakeEmbedder())

    def execute(self, planned, **kwargs):
        trace = ExecutionTrace()
        rows, queries = run(planned, semantic=self.runtime(), trace=trace, **kwargs)
        return rows, queries, trace

    def test_a_shortlist_after_the_required_match_is_ranked_and_reported_as_plan_b(self) -> None:
        planned = self.semantic_plan()
        rows, queries, trace = self.execute(planned)
        anchor = queries[-1]
        self.assertIn("<=>", anchor.sql)
        self.assertIn('"id" IN (', anchor.sql)
        self.assertEqual(trace.reports["semantic"].stats.plan, SemanticPlanKind.VECTOR_SHORTLIST)

    def test_a_learned_set_too_large_to_restrict_the_anchor_falls_back_to_a_plain_scan(self) -> None:
        planned = self.semantic_plan()
        planned = replace(planned, plan=with_assembly(planned.plan, maximum_transfer_keys=1, maximum_key_batches=1))  # 3 learned IDs > 1, never batched
        rows, queries, trace = self.execute(planned)
        anchor = queries[-1]
        self.assertNotIn("<=>", anchor.sql)                  # no unrestricted ranked read
        self.assertEqual(trace.reports["semantic"].stats.plan, SemanticPlanKind.VERIFY_ALL)
        self.assertIsNone(trace.reports["semantic"].stats.shortlisted)
        self.assertEqual({r["id"] for r in rows}, {"t2", "t4", "t6"})  # still the right answer

    def test_the_fallback_scan_keeps_a_row_guard(self) -> None:
        planned = self.semantic_plan()
        planned = replace(planned, plan=with_assembly(planned.plan, maximum_transfer_keys=1, maximum_key_batches=1))
        _, queries, _ = self.execute(planned)
        self.assertEqual(queries[-1].parameters[-1], planned_row_cap(planned) + 1)

    def test_an_anchor_scheduled_before_a_required_contributor_never_uses_the_ranked_read(self) -> None:
        planned = self.semantic_plan()
        anchor_first = (AssemblyStep("helpdesk", StepRole.ANCHOR), AssemblyStep("directory", StepRole.REQUIRED))
        planned = replace(planned, plan=with_assembly(planned.plan, schedule=anchor_first))
        rows, queries, trace = self.execute(planned)
        self.assertNotIn("<=>", queries[0].sql)
        self.assertEqual(trace.reports["semantic"].stats.plan, SemanticPlanKind.VERIFY_ALL)
        self.assertEqual({r["id"] for r in rows}, {"t2", "t4", "t6"})

    def test_a_single_source_shortlist_needs_no_restriction(self) -> None:
        planned = plan(q(sem()), semantic=SemanticPolicy(embedder=EMBEDDER_INFO, embedder_dimensions=3))
        trace = ExecutionTrace()
        _, queries = run(planned, semantic=self.runtime(), trace=trace)
        self.assertIn("<=>", queries[0].sql)
        self.assertEqual(trace.reports["semantic"].stats.plan, SemanticPlanKind.VECTOR_SHORTLIST)


def planned_row_cap(planned) -> int:
    return assembly_of(planned).anchor.vector_search.fallback_maximum_rows


class ScanActualsTests(unittest.TestCase):
    def test_only_complete_unrestricted_reads_are_reported_for_learning(self) -> None:
        planned = plan({"from": {"dataset": "ticket"}, "select": ["subject", "owner"], "where": prio(), "page": {"first": 10}})
        trace = ExecutionTrace()
        run(planned, trace=trace)
        reported = {a.scan.source.source_name: a.rows for a in trace.scan_actuals}
        self.assertEqual(reported, {"helpdesk": 6})  # directory was restricted by the anchor IDs: not a full read

    def test_a_limited_scan_is_not_reported(self) -> None:
        planned = plan(q(prio(), select=("subject",), first=2))
        trace = ExecutionTrace()
        run(planned, trace=trace)
        self.assertEqual(trace.scan_actuals, [])  # a pushed LIMIT truncates the result


if __name__ == "__main__":
    unittest.main()
