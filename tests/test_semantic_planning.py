"""Semantic filter planning: the strategies it offers, the fixed rule, and its cost variants."""

from __future__ import annotations

import unittest
from dataclasses import replace

from yodb.errors import ErrorCode, QueryError
from yodb.operators import OperatorKind
from yodb.planning import ScanOperator, StepRole
from yodb.planning.optimizer import Constraints, CostParameters, Fallback, optimize, Problem, SourceInput
from yodb.planning.statistics import SourceCostProfile
from yodb.query import BoundSemanticPredicate, QueryValidationPolicy, semantic_predicates
from yodb.query.models import BoundPredicate
from yodb.semantic import (
    PowerLawRecall,
    ProviderCost,
    semantic_variants,
    SemanticCosts,
    SemanticOperator,
    SemanticOptions,
    SemanticPlanKind,
    SemanticPlanPreference,
    SemanticPolicy,
    SemanticVerify,
)

from support.tickets import EMBEDDER_INFO, planning_services, prio, q, resolve, sem, semantic_planner


A, B_ = SemanticPlanKind.VERIFY_ALL, SemanticPlanKind.VECTOR_SHORTLIST
PROFILE = SourceCostProfile(call_latency_ms=5.0, per_row_latency_ms=0.01, per_key_latency_ms=0.002)


class OperatorTests(unittest.TestCase):
    def operator(self, **policy):
        return SemanticOperator(planning_services(), SemanticPolicy(embedder=EMBEDDER_INFO, embedder_dimensions=3, **policy))

    def plan_for(self, operator, raw):
        resolved = resolve(raw)
        claim = operator.claim(resolved.query.where)
        core = replace(resolved, query=replace(resolved.query, where=claim.remaining))
        scans = ScanOperator(planning_services()).plan(core, allow_complete=False)
        return operator.plan(claim, core, scans, resolved.query), claim

    def test_it_claims_only_semantic_terms_and_leaves_the_rest_of_the_filter(self) -> None:
        operator = self.operator()
        resolved = resolve(q({"all": [prio(), sem()]}))
        claim = operator.claim(resolved.query.where)
        self.assertEqual(len(claim.terms), 1)
        self.assertIsInstance(claim.terms[0], BoundSemanticPredicate)
        self.assertIsInstance(claim.remaining, BoundPredicate)
        self.assertEqual(semantic_predicates(claim.remaining), ())
        self.assertIsNone(operator.claim(resolve(q(prio())).query.where))
        self.assertIsNone(operator.claim(None))

    def test_a_lone_semantic_term_leaves_no_filter(self) -> None:
        self.assertIsNone(self.operator().claim(resolve(q(sem())).query.where).remaining)

    def test_an_eligible_query_offers_verify_all_and_a_shortlist_ladder_with_the_shortlist_as_default(self) -> None:
        plan, _ = self.plan_for(self.operator(), q(sem(), first=20))
        names = [s.variant.name for s in plan.strategies]
        self.assertEqual(names[0], "verify_all")
        self.assertTrue(all(n.startswith("shortlist:") for n in names[1:]))
        self.assertEqual(plan.default.variant.payload.kind, SemanticPlanKind.VECTOR_SHORTLIST)
        self.assertEqual(plan.default.variant.payload.shortlist_size, 200)   # max(20, 20 x 10)
        self.assertEqual(plan.operator, OperatorKind.SEMANTIC_FILTER)

    def test_without_an_embedder_only_verify_all_is_offered_and_it_is_the_default(self) -> None:
        operator = SemanticOperator(planning_services(), SemanticPolicy())
        plan, _ = self.plan_for(operator, q(sem()))
        self.assertEqual([s.variant.name for s in plan.strategies], ["verify_all"])
        self.assertIs(plan.default, plan.strategies[0])
        self.assertEqual(plan.notes_for(None), ("no embedding provider is configured",))

    def test_preferences_restrict_the_strategies(self) -> None:
        only_all, _ = self.plan_for(self.operator(preference=SemanticPlanPreference.VERIFY_ALL), q(sem()))
        self.assertEqual([s.variant.name for s in only_all.strategies], ["verify_all"])
        only_shortlist, _ = self.plan_for(self.operator(preference=SemanticPlanPreference.VECTOR_SHORTLIST), q(sem()))
        self.assertTrue(all(s.variant.name.startswith("shortlist:") for s in only_shortlist.strategies))

    def test_a_demanded_shortlist_that_is_unavailable_is_a_planning_error(self) -> None:
        operator = SemanticOperator(planning_services(), SemanticPolicy(preference=SemanticPlanPreference.VECTOR_SHORTLIST))
        with self.assertRaises(QueryError) as caught:
            self.plan_for(operator, q(sem()))
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_PLAN_UNSUPPORTED)

    def test_notes_describe_the_fixed_rule_and_any_cost_based_deviation(self) -> None:
        plan, _ = self.plan_for(self.operator(), q(sem(), first=20))
        shortlist = next(s for s in plan.strategies if s.variant.payload.shortlist_size == 400)
        verify_all = plan.strategies[0]
        self.assertEqual(plan.notes_for(None), ("shortlist of 200",))
        self.assertEqual(plan.notes_for(shortlist), ("shortlist of 200", "cost-based: shortlist of 400"))
        self.assertEqual(plan.notes_for(verify_all), ("cost-based: verifying every candidate is cheaper than a shortlist",))

    def test_more_than_one_semantic_term_is_refused_not_silently_dropped(self) -> None:
        # The validator's limit can be raised, but execution handles one: the second term must never vanish.
        two = q({"all": [sem(), sem(proposition="mentions a refund")]})
        resolved = resolve(two, QueryValidationPolicy(limits={"semantic": 2}))
        planner = semantic_planner()
        with self.assertRaises(QueryError) as caught:
            planner.plan(resolved)
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_FEATURE_NOT_SUPPORTED)
        self.assertIn("one semantic condition", caught.exception.detail.message)

    def test_the_built_node_carries_the_chosen_plan_and_notes(self) -> None:
        plan, _ = self.plan_for(self.operator(), q(sem(), first=20))
        scan = ScanOperator(planning_services()).plan(resolve(q(prio())), allow_complete=False).scans[0]
        node = plan.default.build(scan, ("note",))
        self.assertIsInstance(node, SemanticVerify)
        self.assertEqual((node.plan, node.choice_reasons, node.first), (SemanticPlanKind.VECTOR_SHORTLIST, ("note",), 20))
        self.assertEqual((node.embedding_model, node.embedding_dimensions), ("embed-v1", 3))


def anchor(total, filtered=None):
    return SourceInput("anchor", StepRole.ANCHOR, float(total), float(total if filtered is None else filtered), PROFILE, 5_000, None)


def required(name, total, filtered):
    return SourceInput(name, StepRole.REQUIRED, float(total), float(filtered), PROFILE, 5_000, None)


def semantic(**kw):
    base = dict(shortlist_allowed=True, shortlist_start=20, shortlist_cap=1_000, maximum_candidates=1_000, page_size=10, required_recall=0.8)
    return SemanticOptions(**{**base, **kw})


def problem(sources, *, sem, keys=1_000, params=CostParameters(), costs=SemanticCosts(), constraints=Constraints()):
    return Problem(sources, params, maximum_transfer_keys=keys, variants=semantic_variants(sem, costs), constraints=constraints)


def order_of(result):
    return [(s.source_name, s.role.value, s.restrict) for s in result.schedule]


class VariantCostTests(unittest.TestCase):
    def chosen(self, pool, **kw):
        sem = semantic(**{k: v for k, v in kw.items() if k in SemanticOptions.__dataclass_fields__})
        params, costs = kw.get("params", CostParameters()), kw.get("costs", SemanticCosts())
        result = optimize(problem([anchor(pool)], sem=sem, params=params, costs=costs, constraints=kw.get("constraints", Constraints())))
        return result

    def test_a_small_pool_is_verified_in_full(self) -> None:
        self.assertEqual(self.chosen(50).decision.kind, A)

    def test_a_pool_beyond_the_candidate_cap_must_be_shortlisted_when_recall_allows(self) -> None:
        self.assertEqual(self.chosen(1_000_000, required_recall=0.0).decision.kind, B_)

    def test_a_pool_beyond_the_cap_that_no_shortlist_can_cover_declines(self) -> None:
        # (1000 / 1e6) ** 0.5 = 3% expected recall: no shortlist meets 80%, and verifying everything exceeds the cap
        self.assertIsInstance(self.chosen(1_000_000), Fallback)

    def test_a_shortlist_is_chosen_when_it_clearly_saves_work_and_meets_the_recall_requirement(self) -> None:
        result = self.chosen(1_000, page_size=100, shortlist_start=100, required_recall=0.5)
        self.assertEqual(result.decision.kind, B_)
        # (K / 1000) ** 0.5 >= 0.5  <=>  K >= 250, so the cheapest ladder step that qualifies is 400
        self.assertEqual(result.decision.shortlist_size, 400)

    def test_a_shortlist_that_saves_little_loses_to_the_exact_plan(self) -> None:
        # K must be 800 for 80% recall: ~20% fewer verifications, which does not clear the margin
        self.assertEqual(self.chosen(900, page_size=100, shortlist_start=100).decision.kind, A)
        eager = CostParameters(approximation_min_saving=0.0)
        self.assertEqual(self.chosen(900, page_size=100, shortlist_start=100, params=eager).decision.kind, B_)

    def test_the_shortlist_must_beat_the_exact_plan_by_the_margin_but_not_when_that_plan_is_infeasible(self) -> None:
        strict = CostParameters(approximation_min_saving=0.99)
        self.assertEqual(self.chosen(2_000, page_size=100, shortlist_start=100, required_recall=0.5, params=strict).decision.kind, B_)

    def test_a_demanding_recall_requirement_rules_out_the_shortlist(self) -> None:
        result = self.chosen(900, page_size=100, shortlist_start=100, required_recall=1.0, shortlist_cap=800)
        self.assertEqual(result.decision.kind, A)
        self.assertEqual(self.chosen(900, page_size=100, shortlist_start=100, required_recall=1.0, shortlist_cap=1_000).decision.kind, A)

    def test_an_ineligible_shortlist_is_never_chosen(self) -> None:
        self.assertEqual(self.chosen(900, page_size=100, shortlist_allowed=False).decision.kind, A)

    def test_the_money_limit_can_force_the_cheaper_plan_or_decline(self) -> None:
        costs = SemanticCosts(verification=ProviderCost(money_per_candidate=0.01, latency_ms_per_call=1.0, latency_ms_per_candidate=0.1))
        free = self.chosen(1_000, page_size=100, shortlist_start=100, required_recall=0.5, costs=costs)
        self.assertEqual(free.decision.kind, B_)
        tight = self.chosen(1_000, page_size=100, shortlist_start=100, required_recall=0.5, costs=costs, constraints=Constraints(maximum_money=free.estimate.money * 0.99))
        self.assertIsInstance(tight, Fallback)
        generous = self.chosen(1_000, page_size=100, shortlist_start=100, required_recall=0.5, costs=costs, constraints=Constraints(maximum_money=free.estimate.money))
        self.assertEqual(generous.decision, free.decision)

    def test_the_latency_limit_is_a_hard_constraint(self) -> None:
        self.assertIsInstance(self.chosen(900, page_size=100, constraints=Constraints(maximum_latency_ms=1.0)), Fallback)

    def test_a_ranked_read_is_the_last_narrowing_read_and_is_restricted(self) -> None:
        # 4,000 IDs survive the required source (restrictable up to 5,000) and are too many to verify
        sem = semantic(page_size=100, shortlist_start=100, required_recall=0.3)
        p = problem([anchor(1_000_000, 1_000_000), required("owners", 100_000, 4_000)], sem=sem, keys=5_000)
        result = optimize(p)
        self.assertEqual(result.decision.kind, B_)
        self.assertEqual(order_of(result)[-1], ("anchor", "anchor", True))

    def test_a_ranked_read_that_could_not_be_restricted_is_not_offered(self) -> None:
        sem = semantic(page_size=100, shortlist_start=100, maximum_candidates=1_000)
        p = problem([anchor(1_000_000, 1_000_000), required("big", 1_000_000, 500_000)], sem=sem, keys=100)
        result = optimize(p)
        self.assertTrue(isinstance(result, Fallback) or result.decision.kind is A)

    def test_a_filter_the_sources_did_not_enforce_shrinks_the_verified_pool(self) -> None:
        full = self.chosen(500, page_size=500, shortlist_allowed=False)
        half = self.chosen(500, page_size=500, shortlist_allowed=False, unpushed_selectivity=0.5)
        self.assertLess(half.estimate.money, full.estimate.money)

    def test_the_page_filling_early_caps_the_verification_work(self) -> None:
        small_page = self.chosen(900, page_size=1, shortlist_allowed=False)
        big_page = self.chosen(900, page_size=100, shortlist_allowed=False)
        self.assertLess(small_page.estimate.money, big_page.estimate.money)

    def test_a_custom_recall_model_is_honored(self) -> None:
        class Perfect:
            def expected_recall(self, shortlist, pool):
                return 1.0

        result = self.chosen(900, page_size=100, shortlist_start=100, required_recall=1.0, costs=SemanticCosts(recall_model=Perfect()))
        self.assertEqual((result.decision.kind, result.decision.shortlist_size), (B_, 100))


class ValidationTests(unittest.TestCase):
    def test_nonsense_settings_are_rejected(self) -> None:
        for build in (
            lambda: SemanticCosts(expected_selectivity=0),
            lambda: SemanticCosts(minimum_expected_recall=1.5),
            lambda: SemanticCosts(verifier_batch_size=0),
            lambda: SemanticCosts(vector_ranking_per_row_ms=-1),
            lambda: PowerLawRecall(-1),
            lambda: SemanticOptions(True, 0, 10, 10, 1, 0.5),
            lambda: SemanticPolicy(maximum_candidates=0),
            lambda: ProviderCost(money_per_call=-1),
        ):
            with self.assertRaises(ValueError):
                build()

    def test_the_default_recall_model(self) -> None:
        model = PowerLawRecall(0.5)
        self.assertEqual(model.expected_recall(100, 50), 1.0)
        self.assertAlmostEqual(model.expected_recall(25, 100), 0.5)
        self.assertEqual(PowerLawRecall(1.0).expected_recall(10, 100), 0.1)
        self.assertEqual(PowerLawRecall(0.0).expected_recall(1, 1_000_000), 1.0)


if __name__ == "__main__":
    unittest.main()
