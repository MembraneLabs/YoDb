"""Statistics-driven planning end to end: when it deviates, when it declines, and learning."""

from __future__ import annotations

import unittest
from dataclasses import replace

from yodb.catalog import SourceKind
from yodb.compilation import PostgresQueryCompiler, QueryCompilerRegistry
from yodb.execution import QueryExecutionAdapterRegistry, QueryExecutionEngine
from yodb.planning import (
    ColumnStatistics,
    CostParameters,
    FederatedPhysicalPlanner,
    ObservationStore,
    PlannerPolicy,
    RecordAssembly,
    SourcePlanningRegistry,
    SourceStatistics,
    StatisticsService,
)
from yodb.query import bind_query, parse_query, resolve_query_sources
from yodb.semantic import (
    ProviderCost,
    SemanticCosts,
    SemanticExtension,
    SemanticPlanKind,
    SemanticPlanPreference,
    SemanticPolicy,
    SemanticRuntime,
    SemanticVerify,
)

from support.catalogs import SourceRowsExecutor, StaticRuntime
from support.statistics import MapProvider
from support.tickets import (
    EMBEDDER_INFO,
    FakeEmbedder,
    KeywordVerifier,
    prio,
    q,
    sem,
    semantic_planner,
    ticket_catalog,
)


ACTIVE = ticket_catalog()
OWNER = {"field": "owner", "op": "eq", "value": "ann"}




def stats(helpdesk=None, directory=None, **kw):
    table = {}
    if helpdesk is not None:
        table["public.tickets"] = helpdesk
    if directory is not None:
        table["public.owners"] = directory
    return StatisticsService({SourceKind.POSTGRES: MapProvider(table)}, **kw)


def planner(service=None, *, semantic=SemanticPolicy(), costs=CostParameters(), policy=PlannerPolicy()):
    return semantic_planner(semantic, policy=policy, statistics=service, costs=costs)


def plan(raw, service=None, **kw):
    bound = bind_query(parse_query(raw), ACTIVE)
    return planner(service, **kw).plan(resolve_query_sources(bound, ACTIVE))


def assembly(planned):
    node = planned.plan
    while not isinstance(node, RecordAssembly):
        node = node.input
    return node


def verify(planned):
    node = planned.plan
    while not isinstance(node, SemanticVerify):
        node = node.input
    return node


def steps(planned):
    return [(s.source_name, s.role.value, s.restrict) for s in assembly(planned).schedule]


def strategy(planned):
    return planned.explain.optimizer


def shortlist_policy(**kw):
    return SemanticPolicy(embedder=EMBEDDER_INFO, embedder_dimensions=3, **kw)


MULTI = q({"all": [OWNER, {"field": "priority", "op": "eq", "value": 7}]}, select=("subject", "owner"))


class FallbackTests(unittest.TestCase):
    def test_without_statistics_the_fixed_rules_run_and_say_why(self) -> None:
        planned = plan(MULTI)
        self.assertEqual(strategy(planned), ("strategy=rules", "reason=no statistics are configured"))
        self.assertEqual([s[:2] for s in steps(planned)], [("directory", "required"), ("helpdesk", "anchor")])

    def test_a_source_with_unknown_size_makes_the_optimizer_decline_naming_it(self) -> None:
        service = stats(helpdesk=SourceStatistics(row_count=1_000_000))  # directory unknown
        self.assertEqual(
            strategy(plan(MULTI, service)),
            ("strategy=rules", "reason=statistics are unavailable for source 'directory'"),
        )

    def test_a_failing_provider_is_treated_as_unknown_not_as_an_error(self) -> None:
        class Broken:
            def statistics(self, source):
                raise RuntimeError("catalog unreachable")

        planned = plan(MULTI, StatisticsService({SourceKind.POSTGRES: Broken()}))
        self.assertEqual(strategy(planned)[0], "strategy=rules")

    def test_single_source_queries_without_a_semantic_term_have_nothing_to_optimize(self) -> None:
        planned = plan(q(prio(), select=("subject",)), stats(helpdesk=SourceStatistics(row_count=10)))
        self.assertEqual(strategy(planned), ())

    def test_when_no_plan_is_estimated_feasible_the_rules_plan_is_kept(self) -> None:
        both_huge = stats(SourceStatistics(row_count=5_000_000), SourceStatistics(row_count=5_000_000))
        planned = plan(MULTI, both_huge)  # 5% of 5M = 250k rows each: far over the 10,000-row guard
        self.assertEqual(strategy(planned)[0], "strategy=rules")
        self.assertIn("no candidate plan", strategy(planned)[1])
        self.assertEqual([s[:2] for s in steps(planned)], [("directory", "required"), ("helpdesk", "anchor")])

    def test_a_hard_money_limit_nothing_can_meet_keeps_the_rules_and_never_fails_planning(self) -> None:
        service = stats(SourceStatistics(row_count=100_000), SourceStatistics(row_count=100_000))
        raw = q({"all": [OWNER, sem()]}, select=("subject", "owner"), constraints={"maximum_cost": 1e-12})
        planned = plan(raw, service, semantic=shortlist_policy())
        self.assertEqual(strategy(planned)[0], "strategy=rules")


class OrderingDecisionTests(unittest.TestCase):
    PRIORITY = {"priority": ColumnStatistics(distinct_count=1_000_000, null_fraction=0.0)}
    OWNER_STATS = {"owner": ColumnStatistics(distinct_count=2, null_fraction=0.0)}

    def service(self):
        return stats(
            SourceStatistics(row_count=1_000_000, columns=self.PRIORITY),
            SourceStatistics(row_count=5_000_000, columns=self.OWNER_STATS),
        )

    def test_the_optimizer_reads_the_selective_anchor_first_when_the_rules_order_would_overflow(self) -> None:
        planned = plan(MULTI, self.service())
        # owner = 'ann' keeps ~2.5M rows (over the row guard); priority = 7 keeps ~1 row
        self.assertEqual(steps(planned), [("helpdesk", "anchor", False), ("directory", "required", True)])
        self.assertEqual(strategy(planned)[0], "strategy=cost_based")

    def test_the_executor_follows_the_chosen_order_and_restricts_the_big_source_by_the_found_ids(self) -> None:
        executor = SourceRowsExecutor({"helpdesk": ({"id": "t9", "subject": "S9", "priority": 7},), "directory": ({"id": "t9", "owner": "ann"},)})
        engine = QueryExecutionEngine(
            StaticRuntime(ACTIVE),
            QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([executor]),
            statistics=self.service(),
        )
        rows = engine.execute(MULTI).rows
        self.assertEqual([r["id"] for r in rows], ["t9"])
        self.assertEqual([x.source_name for x in executor.queries], ["helpdesk", "directory"])
        self.assertIn('"ticket_id" IN (%s)', executor.queries[1].sql)

    def test_the_plan_differs_from_the_rule_plan_only_in_the_schedule(self) -> None:
        rules = plan(MULTI)
        optimized = plan(MULTI, self.service())
        self.assertNotEqual(rules.plan_fingerprint, optimized.plan_fingerprint)
        self.assertEqual(
            [type(n).__name__ for n in rules.explain.nodes], [type(n).__name__ for n in optimized.explain.nodes]
        )

    def test_when_the_rules_are_already_cheapest_the_plan_is_unchanged(self) -> None:
        service = stats(
            SourceStatistics(row_count=1_000_000),
            SourceStatistics(row_count=5_000, columns={"owner": ColumnStatistics(distinct_count=100, null_fraction=0.0)}),
        )
        planned = plan(q(OWNER, select=("subject", "owner")), service)
        self.assertEqual(planned.explain.optimizer[0], "strategy=cost_based")
        self.assertEqual(steps(planned), [("directory", "required", False), ("helpdesk", "anchor", True)])
        rules = plan(q(OWNER, select=("subject", "owner")))
        # same sources in the same order and roles as the fixed rules (the first read has no IDs to use)
        self.assertEqual([s[:2] for s in steps(planned)], [s[:2] for s in steps(rules)])

    def test_a_non_restrictable_source_is_never_scheduled_as_restricted(self) -> None:
        from yodb.planning import CapabilityPlanningAdapter, POSTGRES_CAPABILITIES

        no_lookup = replace(POSTGRES_CAPABILITIES, key_lookup=None)
        custom = FederatedPhysicalPlanner(
            SourcePlanningRegistry([CapabilityPlanningAdapter(no_lookup)]),
            statistics=stats(SourceStatistics(row_count=10_000), SourceStatistics(row_count=10_000)),
        )
        planned = custom.plan(resolve_query_sources(bind_query(parse_query(q(OWNER, select=("subject", "owner"))), ACTIVE), ACTIVE))
        self.assertTrue(all(not s.restrict for s in assembly(planned).schedule))

    def test_the_explanation_carries_the_estimates_without_any_values(self) -> None:
        notes = plan(MULTI, self.service()).explain.optimizer
        self.assertTrue(any(n.startswith("estimated_latency_ms=") for n in notes))
        self.assertNotIn("ann", " ".join(notes))


class SemanticDecisionTests(unittest.TestCase):
    def tickets(self, rows, **kw):
        return stats(helpdesk=SourceStatistics(row_count=rows, **kw), directory=SourceStatistics(row_count=1))

    def decide(self, raw, rows, policy=None, **kw):
        planned = plan(raw, self.tickets(rows), semantic=policy or shortlist_policy(), **kw)
        return planned, verify(planned)

    def test_a_small_pool_is_verified_in_full_even_though_a_shortlist_is_legal(self) -> None:
        planned, node = self.decide(q(sem()), 200)
        self.assertEqual(node.plan, SemanticPlanKind.VERIFY_ALL)
        self.assertEqual(node.choice_reasons, ("cost-based: verifying every candidate is cheaper than a shortlist",))
        self.assertEqual(strategy(planned)[0], "strategy=cost_based")

    def test_a_pool_too_big_to_verify_gets_the_smallest_shortlist_that_meets_the_quality_bar(self) -> None:
        planned, node = self.decide(q(sem(), first=20, constraints={"minimum_quality": 0.5}), 1_500)
        self.assertEqual(node.plan, SemanticPlanKind.VECTOR_SHORTLIST)
        scan = assembly_or_scan(planned)
        self.assertEqual(scan.vector_search.shortlist_size, 400)   # rules would have used 200: (200/1500)^.5 < .5

    def test_an_unreachable_quality_bar_makes_the_optimizer_decline_and_the_rule_shortlist_stands(self) -> None:
        planned, node = self.decide(q(sem(), first=20), 5_000_000)
        self.assertEqual(strategy(planned)[0], "strategy=rules")
        self.assertEqual(node.plan, SemanticPlanKind.VECTOR_SHORTLIST)   # the rule choice, enforced by guards at run time

    def test_an_explicit_verify_all_preference_is_never_overridden(self) -> None:
        _, node = self.decide(q(sem(), first=20, constraints={"minimum_quality": 0.5}), 1_500, shortlist_policy(preference=SemanticPlanPreference.VERIFY_ALL))
        self.assertEqual(node.plan, SemanticPlanKind.VERIFY_ALL)

    def test_an_explicit_shortlist_preference_is_never_overridden_by_cost(self) -> None:
        _, node = self.decide(q(sem()), 200, shortlist_policy(preference=SemanticPlanPreference.VECTOR_SHORTLIST))
        self.assertEqual(node.plan, SemanticPlanKind.VECTOR_SHORTLIST)

    def test_without_an_embedder_only_verify_all_is_ever_considered(self) -> None:
        planned, node = self.decide(q(sem(), first=20), 1_500, SemanticPolicy())
        self.assertEqual(node.plan, SemanticPlanKind.VERIFY_ALL)

    def test_a_filter_left_to_yodb_makes_the_shortlist_ineligible_regardless_of_cost(self) -> None:
        raw = q({"all": [{"field": "subject", "op": "contains", "value": "x"}, sem()]}, first=20, constraints={"minimum_quality": 0.5})
        _, node = self.decide(raw, 1_500)
        self.assertEqual(node.plan, SemanticPlanKind.VERIFY_ALL)

    def test_provider_cost_hints_and_batch_size_feed_the_cost_model(self) -> None:
        class Hinted(KeywordVerifier):
            cost_hint = ProviderCost(money_per_candidate=7.0, latency_ms_per_call=1.0)

        costs = SemanticExtension(SemanticRuntime(Hinted(), FakeEmbedder(), 4)).costs
        self.assertEqual(costs.verification.money_per_candidate, 7.0)
        self.assertEqual(costs.verifier_batch_size, 3)   # min(runtime cap 4, the verifier's own maximum of 3)
        self.assertEqual(costs.embedding, SemanticCosts().embedding)   # no hint: defaults
        self.assertEqual(SemanticExtension().costs, SemanticCosts())


def assembly_or_scan(planned):
    node = planned.plan
    while hasattr(node, "input"):
        node = node.input
    return node.anchor if isinstance(node, RecordAssembly) else node


class LearningTests(unittest.TestCase):
    def engine(self, service, rows):
        executor = SourceRowsExecutor(rows)
        engine = QueryExecutionEngine(
            StaticRuntime(ACTIVE),
            QueryCompilerRegistry([PostgresQueryCompiler()]),
            QueryExecutionAdapterRegistry([executor]),
            statistics=service,
        )
        return engine, executor

    def test_an_observed_scan_size_corrects_a_wrong_estimate_for_the_next_plan(self) -> None:
        # The catalog claims both tables are huge, so every order looks infeasible: rules run.
        service = stats(SourceStatistics(row_count=5_000_000), SourceStatistics(row_count=5_000_000), observations=ObservationStore())
        rows = {"helpdesk": ({"id": "t1", "subject": "S1", "priority": 7}, {"id": "t2", "subject": "S2", "priority": 7}), "directory": ({"id": "t1", "owner": "ann"},)}
        engine, executor = self.engine(service, rows)
        self.assertEqual(engine.explain(MULTI).optimizer[0], "strategy=rules")
        engine.execute(MULTI)                       # the owners scan really returned 1 row
        after = engine.explain(MULTI).optimizer
        self.assertEqual(after[0], "strategy=cost_based")
        self.assertEqual(len(service.observations), 1)  # only the full, unrestricted scan was learned

    def test_restricted_and_truncated_scans_are_not_learned_from(self) -> None:
        service = stats(SourceStatistics(row_count=100), SourceStatistics(row_count=100), observations=ObservationStore())
        rows = {"helpdesk": ({"id": "t1", "subject": "S1", "priority": 7},), "directory": ({"id": "t1", "owner": "ann"},)}
        engine, _ = self.engine(service, rows)
        engine.execute(q(OWNER, select=("subject", "owner"), first=1))
        learned = {key.source_name for key in service.observations._values}
        self.assertEqual(learned, {"directory"})        # the anchor read was restricted by the found IDs

    def test_executions_without_a_statistics_service_still_work(self) -> None:
        engine, _ = self.engine(None, {"helpdesk": ({"id": "t1", "subject": "S1", "priority": 7},), "directory": ({"id": "t1", "owner": "ann"},)})
        self.assertEqual([r["id"] for r in engine.execute(q(OWNER, select=("subject", "owner"))).rows], ["t1"])


if __name__ == "__main__":
    unittest.main()
