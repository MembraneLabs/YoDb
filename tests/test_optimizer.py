"""The cost model and DP optimizer: optimality, constraints, and edge cases."""

from __future__ import annotations

import math
import random
import unittest

from yodb.planning import StepRole
from yodb.planning.optimizer import (
    Constraints,
    CostParameters,
    Fallback,
    PowerLawRecall,
    Problem,
    SemanticOptions,
    SourceInput,
    brute_force,
    optimize,
)
from yodb.planning.statistics import SourceCostProfile
from yodb.semantic import ProviderCost, SemanticPlanKind

A, B_ = SemanticPlanKind.VERIFY_ALL, SemanticPlanKind.VECTOR_SHORTLIST
PROFILE = SourceCostProfile(call_latency_ms=5.0, per_row_latency_ms=0.01, per_key_latency_ms=0.002)


def src(name, role, total, filtered=None, *, key_limit=5_000, row_cap=None, profile=PROFILE):
    return SourceInput(name, role, float(total), float(total if filtered is None else filtered), profile, key_limit, row_cap)


def anchor(total=1_000_000, filtered=None, **kw):
    return src("anchor", StepRole.ANCHOR, total, filtered, **kw)


def required(name, total, filtered, **kw):
    return src(name, StepRole.REQUIRED, total, filtered, **kw)


def optional(name, total, **kw):
    return src(name, StepRole.OPTIONAL, total, total, **kw)


def semantic(**kw):
    base = dict(shortlist_allowed=True, shortlist_start=20, shortlist_cap=1_000, maximum_candidates=1_000, page_size=10, required_recall=0.8)
    return SemanticOptions(**{**base, **kw})


def problem(sources, *, sem=None, keys=1_000, params=CostParameters(), constraints=Constraints()):
    return Problem(sources, params, maximum_transfer_keys=keys, semantic=sem, constraints=constraints)


def order_of(result):
    return [(s.source_name, s.role.value, s.restrict) for s in result.schedule]


class DpMatchesBruteForceTests(unittest.TestCase):
    def random_problem(self, rng: random.Random) -> Problem:
        total = rng.choice([0, 1, 50, 5_000, 1_000_000])
        sources = [anchor(total, rng.choice([total, total * rng.random()]), key_limit=rng.choice([None, 200, 5_000]),
                          row_cap=rng.choice([None, 10_000]))]
        for i in range(rng.randint(0, 3)):
            t = rng.choice([1, 100, 20_000, 3_000_000])
            sources.append(required(f"r{i}", t, t * rng.random(), key_limit=rng.choice([None, 50, 5_000]), row_cap=rng.choice([None, 10_000, 100_000]),
                                    profile=SourceCostProfile(rng.uniform(1, 50), rng.uniform(0.001, 0.05), rng.uniform(0.0005, 0.01))))
        for i in range(rng.randint(0, 2)):
            sources.append(optional(f"o{i}", rng.choice([10, 5_000, 500_000]), key_limit=rng.choice([None, 1_000, 5_000]), row_cap=rng.choice([None, 20_000])))
        sem = None
        if rng.random() < 0.7:
            sem = semantic(shortlist_allowed=rng.random() < 0.7, shortlist_start=rng.choice([5, 20, 100]), shortlist_cap=rng.choice([100, 1_000]),
                           maximum_candidates=rng.choice([500, 1_000, 5_000]), page_size=rng.choice([1, 10, 100]), required_recall=rng.choice([0.0, 0.5, 0.8, 0.99]))
            sem = SemanticOptions(**{**sem.__dict__, "shortlist_cap": max(sem.shortlist_cap, sem.shortlist_start)})
        constraints = Constraints(maximum_money=rng.choice([None, 0.05, 1.0]), maximum_latency_ms=rng.choice([None, 2_000.0, 1e9]))
        return problem(sources, sem=sem, keys=rng.choice([None, 100, 1_000, 5_000]), constraints=constraints)

    def test_the_dp_finds_the_same_optimum_as_trying_everything(self) -> None:
        rng = random.Random(20261003)
        compared = infeasible = 0
        for trial in range(400):
            p = self.random_problem(rng)
            expected = brute_force(p)
            result = optimize(p)
            with self.subTest(trial=trial):
                if expected is None:
                    self.assertIsInstance(result, Fallback)
                    infeasible += 1
                    continue
                self.assertNotIsInstance(result, Fallback)
                self.assertAlmostEqual(result.objective, expected[0], delta=1e-6 * max(1.0, expected[0]))
                compared += 1
        self.assertGreater(compared, 100)      # the sample is not vacuous
        self.assertGreater(infeasible, 5)      # and exercises the "nothing feasible" path

    def test_the_reported_estimate_is_what_the_chosen_plan_actually_costs(self) -> None:
        rng = random.Random(7)
        checked = 0
        for _ in range(200):
            p = self.random_problem(rng)
            result = optimize(p)
            if isinstance(result, Fallback):
                continue
            narrowing = [(s.source_name, s.restrict) for s in result.schedule if s.role is not StepRole.OPTIONAL]
            kind = result.semantic.kind if result.semantic else None
            size = result.semantic.shortlist_size if result.semantic else None
            estimate = p.evaluate(narrowing, kind, size)
            self.assertIsNotNone(estimate)
            self.assertAlmostEqual(estimate.latency_ms, result.estimate.latency_ms, delta=1e-6 * max(1.0, estimate.latency_ms))
            self.assertAlmostEqual(estimate.money, result.estimate.money, places=9)
            checked += 1
        self.assertGreater(checked, 50)

    def test_the_result_is_deterministic(self) -> None:
        rng = random.Random(3)
        for _ in range(50):
            p = self.random_problem(rng)
            self.assertEqual(optimize(p), optimize(p))


class OrderingTests(unittest.TestCase):
    def test_a_selective_required_contributor_is_read_first_and_restricts_the_anchor(self) -> None:
        p = problem([anchor(1_000_000), required("owners", 5_000, 50)])
        result = optimize(p)
        self.assertEqual(order_of(result), [("owners", "required", False), ("anchor", "anchor", True)])

    def test_a_selective_anchor_filter_goes_first_and_restricts_the_contributor(self) -> None:
        p = problem([anchor(1_000_000, 20), required("big", 2_000_000, 1_000_000, row_cap=100_000)])
        result = optimize(p)
        self.assertEqual(order_of(result), [("anchor", "anchor", False), ("big", "required", True)])

    def test_restriction_is_dropped_when_the_learned_set_exceeds_the_bound(self) -> None:
        p = problem([anchor(100_000, 100_000), required("r", 100_000, 50_000)], keys=100)
        result = optimize(p)
        self.assertTrue(all(not s.restrict for s in result.schedule))   # 50,000 IDs cannot restrict anything

    def test_a_source_that_cannot_be_restricted_is_never_restricted(self) -> None:
        p = problem([anchor(10_000, key_limit=None), required("r", 10_000, 10, key_limit=None)])
        self.assertTrue(all(not s.restrict for s in optimize(p).schedule))

    def test_the_source_with_the_cheaper_calls_is_not_preferred_blindly(self) -> None:
        slow = SourceCostProfile(500.0, 0.01, 0.002)
        p = problem([anchor(1_000, 1_000), required("far", 1_000, 1_000, profile=slow), required("near", 1_000, 1_000)])
        names = [s.source_name for s in optimize(p).schedule]
        self.assertEqual(sorted(names), ["anchor", "far", "near"])   # every source is read exactly once

    def test_every_source_appears_once_with_a_consistent_role(self) -> None:
        p = problem([anchor(), required("r1", 100, 10), required("r2", 100, 10), optional("o1", 100)])
        steps = optimize(p).schedule
        self.assertEqual(sorted(s.source_name for s in steps), ["anchor", "o1", "r1", "r2"])
        self.assertEqual(steps[-1].role, StepRole.OPTIONAL)           # enrichers are always last
        self.assertEqual(sum(s.role is StepRole.ANCHOR for s in steps), 1)

    def test_enrichers_are_restricted_by_the_final_id_set_when_it_is_small(self) -> None:
        p = problem([anchor(1_000_000, 50), optional("o", 2_000_000)])
        self.assertTrue(optimize(p).schedule[-1].restrict)
        big = problem([anchor(1_000_000, 900_000), optional("o", 900_000)], keys=1_000)
        self.assertFalse(optimize(big).schedule[-1].restrict)


class FeasibilityTests(unittest.TestCase):
    def test_a_scan_that_would_exceed_its_row_cap_is_avoided_by_restricting(self) -> None:
        p = problem([anchor(1_000_000, 1_000_000, row_cap=10_000), required("r", 100_000, 40)])
        result = optimize(p)
        self.assertEqual(order_of(result)[-1], ("anchor", "anchor", True))   # the anchor can only be read narrowed

    def test_nothing_feasible_declines(self) -> None:
        p = problem([anchor(1_000_000, 1_000_000, row_cap=10_000, key_limit=None)])
        self.assertIsInstance(optimize(p), Fallback)

    def test_an_oversized_enricher_with_a_row_cap_declines_when_it_cannot_be_restricted(self) -> None:
        p = problem([anchor(1_000_000, 900_000), optional("o", 900_000, row_cap=1_000)])
        self.assertIsInstance(optimize(p), Fallback)

    def test_empty_tables_do_not_break_the_arithmetic(self) -> None:
        for sources in ([anchor(0, 0)], [anchor(0, 0), required("r", 0, 0)], [anchor(100, 0), required("r", 50, 0), optional("o", 0)]):
            result = optimize(problem(sources))
            self.assertNotIsInstance(result, Fallback)
            self.assertTrue(math.isfinite(result.objective))

    def test_the_candidate_set_size_limit_is_enforced(self) -> None:
        self.assertIsInstance(optimize(problem([anchor(2_000, 2_000)], sem=semantic(shortlist_allowed=False, maximum_candidates=1_000))), Fallback)

    def test_too_many_sources_decline_with_a_reason(self) -> None:
        sources = [anchor(), *[required(f"r{i}", 100, 10) for i in range(11)]]
        result = optimize(problem(sources, params=CostParameters(maximum_dp_sources=10)))
        self.assertIsInstance(result, Fallback)
        self.assertIn("exceed the limit", result.reason)


class SemanticChoiceTests(unittest.TestCase):
    def chosen(self, pool, **kw):
        sem = semantic(**{k: v for k, v in kw.items() if k in SemanticOptions.__dataclass_fields__})
        params = kw.get("params", CostParameters())
        result = optimize(problem([anchor(pool, pool)], sem=sem, params=params, constraints=kw.get("constraints", Constraints())))
        return result

    def test_a_small_pool_is_verified_in_full(self) -> None:
        self.assertEqual(self.chosen(50).semantic.kind, A)

    def test_a_pool_beyond_the_candidate_cap_must_be_shortlisted_when_recall_allows(self) -> None:
        self.assertEqual(self.chosen(1_000_000, required_recall=0.0).semantic.kind, B_)

    def test_a_pool_beyond_the_cap_that_no_shortlist_can_cover_declines(self) -> None:
        # (1000 / 1e6) ** 0.5 = 3% expected recall: no shortlist meets 80%, and verifying everything exceeds the cap
        self.assertIsInstance(self.chosen(1_000_000), Fallback)

    def test_a_shortlist_is_chosen_when_it_clearly_saves_work_and_meets_the_recall_requirement(self) -> None:
        result = self.chosen(1_000, page_size=100, shortlist_start=100, required_recall=0.5)
        self.assertEqual(result.semantic.kind, B_)
        # (K / 1000) ** 0.5 >= 0.5  <=>  K >= 250, so the cheapest ladder step that qualifies is 400
        self.assertEqual(result.semantic.shortlist_size, 400)

    def test_a_shortlist_that_saves_little_loses_to_the_exact_plan(self) -> None:
        # K must be 800 for 80% recall: ~20% fewer verifications, which does not clear the margin
        self.assertEqual(self.chosen(900, page_size=100, shortlist_start=100).semantic.kind, A)
        eager = CostParameters(shortlist_min_saving=0.0)
        self.assertEqual(self.chosen(900, page_size=100, shortlist_start=100, params=eager).semantic.kind, B_)

    def test_the_shortlist_must_beat_the_exact_plan_by_the_margin_but_not_when_that_plan_is_infeasible(self) -> None:
        strict = CostParameters(shortlist_min_saving=0.99)
        self.assertEqual(self.chosen(2_000, page_size=100, shortlist_start=100, required_recall=0.5, params=strict).semantic.kind, B_)

    def test_a_demanding_recall_requirement_rules_out_the_shortlist(self) -> None:
        result = self.chosen(900, page_size=100, shortlist_start=100, required_recall=1.0, shortlist_cap=800)
        self.assertEqual(result.semantic.kind, A)
        self.assertEqual(self.chosen(900, page_size=100, shortlist_start=100, required_recall=1.0, shortlist_cap=1_000).semantic.kind, A)

    def test_an_ineligible_shortlist_is_never_chosen(self) -> None:
        self.assertEqual(self.chosen(900, page_size=100, shortlist_allowed=False).semantic.kind, A)

    def test_the_money_limit_can_force_the_cheaper_plan_or_decline(self) -> None:
        params = CostParameters(verification=ProviderCost(money_per_candidate=0.01, latency_ms_per_call=1.0, latency_ms_per_candidate=0.1))
        free = self.chosen(1_000, page_size=100, shortlist_start=100, required_recall=0.5, params=params)
        self.assertEqual(free.semantic.kind, B_)
        tight = self.chosen(1_000, page_size=100, shortlist_start=100, required_recall=0.5, params=params, constraints=Constraints(maximum_money=free.estimate.money * 0.99))
        self.assertIsInstance(tight, Fallback)
        generous = self.chosen(1_000, page_size=100, shortlist_start=100, required_recall=0.5, params=params, constraints=Constraints(maximum_money=free.estimate.money))
        self.assertEqual(generous.semantic, free.semantic)

    def test_the_latency_limit_is_a_hard_constraint(self) -> None:
        self.assertIsInstance(self.chosen(900, page_size=100, constraints=Constraints(maximum_latency_ms=1.0)), Fallback)

    def test_a_ranked_read_is_the_last_narrowing_read_and_is_restricted(self) -> None:
        # 4,000 IDs survive the required source (restrictable up to 5,000) and are too many to verify
        sem = semantic(page_size=100, shortlist_start=100, required_recall=0.3)
        p = problem([anchor(1_000_000, 1_000_000), required("owners", 100_000, 4_000)], sem=sem, keys=5_000)
        result = optimize(p)
        self.assertEqual(result.semantic.kind, B_)
        self.assertEqual(order_of(result)[-1], ("anchor", "anchor", True))

    def test_a_ranked_read_that_could_not_be_restricted_is_not_offered(self) -> None:
        sem = semantic(page_size=100, shortlist_start=100, maximum_candidates=1_000)
        p = problem([anchor(1_000_000, 1_000_000), required("big", 1_000_000, 500_000)], sem=sem, keys=100)
        result = optimize(p)
        self.assertTrue(isinstance(result, Fallback) or result.semantic.kind is A)

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

        result = self.chosen(900, page_size=100, shortlist_start=100, required_recall=1.0, params=CostParameters(recall_model=Perfect()))
        self.assertEqual((result.semantic.kind, result.semantic.shortlist_size), (B_, 100))


class RuleTieBreakTests(unittest.TestCase):
    def test_an_exact_tie_keeps_the_planners_own_plan(self) -> None:
        sources = [anchor(1_000, 1_000), required("a", 1_000, 1_000), required("b", 1_000, 1_000)]
        p = problem(sources)
        self.assertEqual([s.source_name for s in optimize(p, rule_order=["b", "a", "anchor"]).schedule][:3], ["b", "a", "anchor"])
        self.assertEqual([s.source_name for s in optimize(p, rule_order=["a", "b", "anchor"]).schedule][:3], ["a", "b", "anchor"])

    def test_it_deviates_from_the_rules_only_when_strictly_cheaper(self) -> None:
        p = problem([anchor(1_000_000), required("owners", 5_000, 50)])
        self.assertEqual(optimize(p, rule_order=["anchor", "owners"]).schedule[0].source_name, "owners")


class ParameterValidationTests(unittest.TestCase):
    def test_nonsense_parameters_are_rejected(self) -> None:
        for build in (
            lambda: CostParameters(money_weight=-1),
            lambda: CostParameters(expected_semantic_selectivity=0),
            lambda: CostParameters(minimum_expected_recall=1.5),
            lambda: CostParameters(shortlist_min_saving=1.0),
            lambda: CostParameters(verifier_batch_size=0),
            lambda: PowerLawRecall(-1),
            lambda: SemanticOptions(True, 0, 10, 10, 1, 0.5),
            lambda: ProviderCost(money_per_call=-1),
            lambda: src("x", StepRole.ANCHOR, -1),
            lambda: problem([required("r", 1, 1)]),                       # no anchor
            lambda: problem([anchor(), anchor()]),                        # duplicate names
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
