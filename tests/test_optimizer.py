"""The cost model and DP optimizer: optimality, constraints, and edge cases.

The optimizer knows nothing about what an extension does, so these tests use toy
variants: an exact one (cost per candidate, optional cap) and a ranked,
approximate one (reads only the top ``size`` rows).
"""

from __future__ import annotations

import math
import random
import unittest

from yodb.planning import StepRole
from yodb.planning.optimizer import (
    brute_force,
    Constraints,
    CostParameters,
    Estimate,
    Fallback,
    optimize,
    Problem,
    RankedRead,
    SourceInput,
    Variant,
)
from yodb.planning.statistics import SourceCostProfile


PROFILE = SourceCostProfile(call_latency_ms=5.0, per_row_latency_ms=0.01, per_key_latency_ms=0.002)


def src(name, role, total, filtered=None, *, key_limit=5_000, row_cap=None, profile=PROFILE):
    return SourceInput(name, role, float(total), float(total if filtered is None else filtered), profile, key_limit, row_cap)


def anchor(total=1_000_000, filtered=None, **kw):
    return src("anchor", StepRole.ANCHOR, total, filtered, **kw)


def required(name, total, filtered, **kw):
    return src(name, StepRole.REQUIRED, total, filtered, **kw)


def optional(name, total, **kw):
    return src(name, StepRole.OPTIONAL, total, total, **kw)


def exact(per_row_ms=1.0, *, cap=None, money_per_row=0.0):
    """Handles every candidate it is given (None when there are more than ``cap``)."""

    def cost(candidates):
        if cap is not None and candidates.count > cap:
            return None
        return Estimate(per_row_ms * candidates.count, money_per_row * candidates.count)

    return Variant("exact", cost=cost)


def ranked(size, per_row_ms=1.0, *, money_per_row=0.0, min_coverage=0.0):
    """Approximate: demands a ranked read of the top ``size``; unusable if it covers too little of the pool."""

    def cost(candidates):
        if candidates.pool > 0 and size / candidates.pool < min_coverage:
            return None
        return Estimate(per_row_ms * candidates.count, money_per_row * candidates.count)

    return Variant(f"ranked:{size}", cost=cost, demand=RankedRead(size, 0.002), approximate=True)


def problem(sources, *, variants=(), keys=1_000, params=CostParameters(), constraints=Constraints()):
    return Problem(sources, params, maximum_transfer_keys=keys, variants=variants, constraints=constraints)


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
        variants = []
        if rng.random() < 0.7:
            if rng.random() < 0.8:
                variants.append(exact(rng.choice([0.05, 1.0]), cap=rng.choice([None, 500, 5_000]), money_per_row=rng.choice([0.0, 0.001])))
            if rng.random() < 0.7:
                for size in sorted(rng.sample([5, 20, 100, 1_000], k=rng.randint(1, 3))):
                    variants.append(ranked(size, rng.choice([0.05, 1.0]), money_per_row=rng.choice([0.0, 0.001]), min_coverage=rng.choice([0.0, 0.01, 0.5])))
        constraints = Constraints(maximum_money=rng.choice([None, 0.05, 1.0]), maximum_latency_ms=rng.choice([None, 2_000.0, 1e9]))
        return problem(sources, variants=variants, keys=rng.choice([None, 100, 1_000, 5_000]), constraints=constraints)

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
            estimate = p.evaluate(narrowing, result.variant)
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

    def test_a_variants_own_limit_is_enforced(self) -> None:
        self.assertIsInstance(optimize(problem([anchor(2_000, 2_000)], variants=[exact(cap=1_000)])), Fallback)

    def test_too_many_sources_decline_with_a_reason(self) -> None:
        sources = [anchor(), *[required(f"r{i}", 100, 10) for i in range(11)]]
        result = optimize(problem(sources, params=CostParameters(maximum_dp_sources=10)))
        self.assertIsInstance(result, Fallback)
        self.assertIn("exceed the limit", result.reason)


class VariantChoiceTests(unittest.TestCase):
    def chosen(self, pool, *variants, **kw):
        return optimize(problem([anchor(pool, pool)], variants=variants, **kw))

    def test_the_cheaper_variant_is_chosen(self) -> None:
        self.assertEqual(self.chosen(1_000, exact(1.0), exact(0.5)).variant.name, "exact")
        result = self.chosen(1_000, ranked(100, 0.1), exact(1.0), params=CostParameters(approximation_min_saving=0.0))
        self.assertEqual(result.variant.name, "ranked:100")

    def test_a_pool_beyond_the_exact_cap_must_use_the_ranked_read(self) -> None:
        self.assertEqual(self.chosen(1_000_000, exact(cap=1_000), ranked(1_000)).variant.name, "ranked:1000")

    def test_nothing_feasible_declines(self) -> None:
        self.assertIsInstance(self.chosen(1_000_000, exact(cap=1_000), ranked(1_000, min_coverage=0.5)), Fallback)

    def test_an_approximate_variant_that_saves_little_loses_to_the_exact_one(self) -> None:
        # ranked reads 800 rows against 900: ~11% less work, which does not clear the default 20% margin
        self.assertEqual(self.chosen(900, exact(1.0), ranked(800)).variant.name, "exact")
        eager = CostParameters(approximation_min_saving=0.0)
        self.assertEqual(self.chosen(900, exact(1.0), ranked(800), params=eager).variant.name, "ranked:800")

    def test_the_margin_is_not_applied_when_the_exact_variant_is_infeasible(self) -> None:
        strict = CostParameters(approximation_min_saving=0.99)
        self.assertEqual(self.chosen(2_000, exact(cap=1_000), ranked(800), params=strict).variant.name, "ranked:800")

    def test_a_variant_that_declines_its_own_requirement_is_never_chosen(self) -> None:
        self.assertEqual(self.chosen(900, exact(1.0), ranked(100, 0.01, min_coverage=0.5)).variant.name, "exact")

    def test_the_money_limit_can_force_the_cheaper_plan_or_decline(self) -> None:
        variants = (exact(0.1, money_per_row=0.01), ranked(400, 0.1, money_per_row=0.01))
        eager = CostParameters(approximation_min_saving=0.0)
        free = self.chosen(1_000, *variants, params=eager)
        self.assertEqual(free.variant.name, "ranked:400")
        tight = self.chosen(1_000, *variants, params=eager, constraints=Constraints(maximum_money=free.estimate.money * 0.99))
        self.assertIsInstance(tight, Fallback)
        generous = self.chosen(1_000, *variants, params=eager, constraints=Constraints(maximum_money=free.estimate.money))
        self.assertEqual(generous.variant.name, free.variant.name)

    def test_the_latency_limit_is_a_hard_constraint(self) -> None:
        self.assertIsInstance(self.chosen(900, exact(1.0), constraints=Constraints(maximum_latency_ms=1.0)), Fallback)

    def test_a_ranked_read_is_the_last_narrowing_read_and_is_restricted(self) -> None:
        # 4,000 IDs survive the required source (restrictable up to 5,000) and are too many for the exact variant
        p = problem([anchor(1_000_000, 1_000_000), required("owners", 100_000, 4_000)], variants=[exact(cap=1_000), ranked(100)], keys=5_000)
        result = optimize(p)
        self.assertEqual(result.variant.name, "ranked:100")
        self.assertEqual(order_of(result)[-1], ("anchor", "anchor", True))

    def test_a_ranked_read_that_could_not_be_restricted_is_not_offered(self) -> None:
        p = problem([anchor(1_000_000, 1_000_000), required("big", 1_000_000, 500_000)], variants=[exact(cap=1_000), ranked(100)], keys=100)
        self.assertIsInstance(optimize(p), Fallback)

    def test_the_decision_is_the_chosen_variants_payload(self) -> None:
        variant = Variant("tagged", cost=lambda c: Estimate(1.0, 0.0), payload={"why": "toy"})
        self.assertEqual(self.chosen(10, variant).decision, {"why": "toy"})
        self.assertIsNone(optimize(problem([anchor(10, 10)])).decision)


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
            lambda: CostParameters(approximation_min_saving=1.0),
            lambda: CostParameters(unpushed_filter_selectivity=0),
            lambda: CostParameters(maximum_dp_sources=0),
            lambda: RankedRead(0),
            lambda: RankedRead(5, -1.0),
            lambda: src("x", StepRole.ANCHOR, -1),
            lambda: problem([required("r", 1, 1)]),                       # no anchor
            lambda: problem([anchor(), anchor()]),                        # duplicate names
        ):
            with self.assertRaises(ValueError):
                build()


if __name__ == "__main__":
    unittest.main()
