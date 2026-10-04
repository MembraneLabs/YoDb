"""Cost-based choice of how to read sources and answer a semantic term.

The planner always has a correct plan from its fixed rules.  This module finds a
*cheaper* one when it has enough statistics, and says nothing otherwise.

What is chosen
--------------
* the order in which the sources that bound the result (the anchor and the
  required contributors) are read, and for each read whether the IDs learned so
  far should restrict it;
* whether enriching (optional) contributors are restricted by the final ID set;
* how the semantic term is answered: verify everything (Plan A), or verify a
  shortlist of K ranked rows (Plan B) for the cheapest K that meets the quality
  requirement.

Why dynamic programming works here
----------------------------------
Under the usual independence assumption the number of IDs known after reading a
*set* of sources does not depend on the order they were read in, so the cheapest
way to have read a set is the cheapest way to have read a smaller set plus the
cost of the last read.  That is a DP over subsets: ``2^m * m`` steps instead of
``m!`` orders.  The semantic term does not disturb it: verification cost depends
only on the final candidate count (order-independent), and a shortlist is only
valid as the *last* narrowing read, which is one extra transition.

Everything is a pure function of :class:`SourceInput` and :class:`CostParameters`,
so a different cost term, recall model or source behavior is a local change, and
the brute-force check in the tests uses the very same cost function.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
import itertools
import math
from typing import Protocol

from ..semantic import ProviderCost, SemanticPlanKind
from .contracts import AssemblyStep, StepRole
from .statistics import SourceCostProfile

INFINITY = math.inf


class RecallModel(Protocol):
    """Expected fraction of true matches a shortlist of ``shortlist`` rows finds.

    ``pool`` is how many rows the ranking chooses from.  Return 1.0 when the
    shortlist covers the pool.  Swap this for a model fitted to real data.
    """

    def expected_recall(self, shortlist: float, pool: float) -> float: ...


@dataclass(frozen=True)
class PowerLawRecall:
    """``(K / pool) ** exponent``: exponent 1 is a random ranking, 0 is perfect."""

    exponent: float = 0.5

    def __post_init__(self) -> None:
        if not (math.isfinite(self.exponent) and self.exponent >= 0):
            raise ValueError("exponent must be a finite non-negative number")

    def expected_recall(self, shortlist: float, pool: float) -> float:
        if pool <= shortlist or pool <= 0:
            return 1.0
        return (shortlist / pool) ** self.exponent


@dataclass(frozen=True)
class CostParameters:
    """Tunable weights and assumptions of the cost model."""

    # Latency-equivalent of one unit of money: objective = latency_ms + this * money.
    money_weight: float = 1_000.0
    verification: ProviderCost = ProviderCost(
        money_per_call=0.0, money_per_candidate=0.001, latency_ms_per_call=300.0, latency_ms_per_candidate=20.0
    )
    embedding: ProviderCost = ProviderCost(money_per_call=0.0001, latency_ms_per_call=80.0)
    verifier_batch_size: int = 10
    # Fraction of candidates expected to satisfy the proposition (sets how soon a page fills).
    expected_semantic_selectivity: float = 0.1
    # Recall a shortlist must be expected to reach when the caller gave no quality bar.
    minimum_expected_recall: float = 0.8
    # Plan B is approximate, so it must beat the exact plan by this fraction to be
    # preferred (its cost is divided by ``1 - margin`` when compared).  Not applied
    # when verifying everything is infeasible.
    shortlist_min_saving: float = 0.2
    # Extra cost of ranking one row by vector distance.
    vector_ranking_per_row_ms: float = 0.002
    # Fraction of rows kept by filter terms the sources did not enforce.
    unpushed_filter_selectivity: float = 0.5
    maximum_dp_sources: int = 10
    recall_model: RecallModel = field(default_factory=PowerLawRecall)

    def __post_init__(self) -> None:
        for name in ("money_weight", "vector_ranking_per_row_ms"):
            value = getattr(self, name)
            if not (math.isfinite(value) and value >= 0):
                raise ValueError(f"{name} must be a finite non-negative number")
        for name in ("expected_semantic_selectivity", "unpushed_filter_selectivity"):
            if not 0.0 < getattr(self, name) <= 1.0:
                raise ValueError(f"{name} must be within (0, 1]")
        if not 0.0 <= self.minimum_expected_recall <= 1.0:
            raise ValueError("minimum_expected_recall must be within [0, 1]")
        if not 0.0 <= self.shortlist_min_saving < 1.0:
            raise ValueError("shortlist_min_saving must be within [0, 1)")
        if self.verifier_batch_size < 1 or self.maximum_dp_sources < 1:
            raise ValueError("verifier_batch_size and maximum_dp_sources must be positive")


@dataclass(frozen=True)
class SourceInput:
    """Everything the cost model needs to know about one source's scan."""

    name: str
    role: StepRole
    total_rows: float
    filtered_rows: float          # rows its pushed filter keeps (an unrestricted scan's size)
    profile: SourceCostProfile
    key_limit: int | None         # largest ID set it accepts as a restriction (None: never)
    row_cap: int | None           # an unrestricted scan above this fails at run time

    def __post_init__(self) -> None:
        if self.total_rows < 0 or self.filtered_rows < 0:
            raise ValueError("row estimates must not be negative")


@dataclass(frozen=True)
class SemanticOptions:
    """What the planner knows about answering the semantic term."""

    shortlist_allowed: bool       # Plan B is legal (eligible, anchor is the text's source...)
    shortlist_start: int          # smallest shortlist worth considering
    shortlist_cap: int            # largest allowed (candidate cap and the source's own limit)
    maximum_candidates: int       # more than this reaching the verifier fails at run time
    page_size: int
    required_recall: float
    unpushed_selectivity: float = 1.0   # < 1 when part of the filter still runs in YoDb
    verify_all_allowed: bool = True     # False when the caller insisted on a shortlist

    def __post_init__(self) -> None:
        if min(self.shortlist_start, self.shortlist_cap, self.maximum_candidates, self.page_size) < 1:
            raise ValueError("semantic sizes must be positive")


@dataclass(frozen=True)
class Constraints:
    """Hard limits from the caller; a plan estimated to break one is not chosen."""

    maximum_money: float | None = None
    maximum_latency_ms: float | None = None


@dataclass(frozen=True)
class Estimate:
    latency_ms: float
    money: float

    def objective(self, params: CostParameters) -> float:
        return self.latency_ms + params.money_weight * self.money

    def within(self, constraints: Constraints) -> bool:
        return (constraints.maximum_money is None or self.money <= constraints.maximum_money) and (
            constraints.maximum_latency_ms is None or self.latency_ms <= constraints.maximum_latency_ms
        )


@dataclass(frozen=True)
class SemanticDecision:
    kind: SemanticPlanKind
    shortlist_size: int | None


@dataclass(frozen=True)
class OptimizerResult:
    schedule: tuple[AssemblyStep, ...]
    semantic: SemanticDecision | None
    estimate: Estimate
    objective: float
    candidates_considered: int


@dataclass(frozen=True)
class Fallback:
    """The optimizer declined; the planner keeps its fixed-rule plan."""

    reason: str


@dataclass(frozen=True)
class _Read:
    latency: float
    rows: float


class Problem:
    """One optimization instance: sources, assumptions and the shared cost function."""

    def __init__(
        self,
        sources: Sequence[SourceInput],
        params: CostParameters,
        *,
        maximum_transfer_keys: int | None,
        semantic: SemanticOptions | None = None,
        constraints: Constraints = Constraints(),
    ) -> None:
        anchors = [s for s in sources if s.role is StepRole.ANCHOR]
        if len(anchors) != 1 or len({s.name for s in sources}) != len(sources):
            raise ValueError("exactly one anchor and unique source names are required")
        self.params = params
        self.transfer_bound = maximum_transfer_keys
        self.semantic = semantic
        self.constraints = constraints
        self.anchor = anchors[0]
        self.required = [s for s in sources if s.role is StepRole.REQUIRED]
        self.optional = [s for s in sources if s.role is StepRole.OPTIONAL]
        self.narrowing = [self.anchor, *self.required]          # anchor is index 0
        self.universe = max(self.anchor.total_rows, 0.0)

    # --- the shared cost function ------------------------------------------------

    def pass_fraction(self, source: SourceInput) -> float:
        """Probability an ID in the universe survives this source (independence)."""

        if self.universe <= 0:
            return 0.0
        if source.role is StepRole.OPTIONAL:       # enrichers keep every ID; this is coverage
            return min(1.0, source.total_rows / self.universe)
        return min(1.0, source.filtered_rows / self.universe)

    def bound(self, source: SourceInput) -> int | None:
        if self.transfer_bound is None or source.key_limit is None:
            return None
        return min(self.transfer_bound, source.key_limit)

    def plain_read(self, source: SourceInput, keys_in: float | None, restrict: bool) -> _Read | None:
        """Cost and result size of a plain read, or None if it cannot (or must not) run."""

        profile = source.profile
        if restrict:
            bound = self.bound(source)
            if keys_in is None or bound is None or keys_in > bound:
                return None
            rows = keys_in * self.pass_fraction(source)
            latency = profile.call_latency_ms + profile.per_key_latency_ms * keys_in + profile.per_row_latency_ms * rows
        else:
            rows = source.filtered_rows
            latency = profile.call_latency_ms + profile.per_row_latency_ms * rows
        if source.row_cap is not None and rows > source.row_cap:
            return None
        return _Read(latency, rows)

    def best_read(self, source: SourceInput, keys_in: float | None) -> tuple[_Read, bool] | None:
        """The cheaper of restricted and unrestricted (restricted on a tie, as the rules do)."""

        restricted = self.plain_read(source, keys_in, True)
        plain = self.plain_read(source, keys_in, False)
        if restricted is not None and (plain is None or restricted.latency <= plain.latency):
            return restricted, True
        if plain is not None:
            return plain, False
        return None

    def keys_after(self, names: frozenset[str]) -> float:
        """Estimated IDs known after reading these narrowing sources (order-independent)."""

        keys = self.universe
        for source in self.narrowing:
            if source.name in names:
                keys *= self.pass_fraction(source)
        return keys

    def ranked_read(self, keys_in: float | None, shortlist: int) -> tuple[_Read, float, float] | None:
        """Cost, result size, pool size and recall inputs for a ranked anchor read."""

        anchor = self.anchor
        restrict = bool(self.required)
        if restrict:
            bound = self.bound(anchor)
            if keys_in is None or bound is None or keys_in > bound:
                return None            # a ranked read cut before the required matches is unsafe
            pool = keys_in * self.pass_fraction(anchor)
        else:
            pool = anchor.filtered_rows
        rows = min(float(shortlist), pool)
        profile = anchor.profile
        latency = (
            profile.call_latency_ms
            + (profile.per_key_latency_ms * keys_in if restrict and keys_in else 0.0)
            + profile.per_row_latency_ms * rows
            + self.params.vector_ranking_per_row_ms * pool
        )
        return _Read(latency, rows), pool, rows

    def optional_cost(self, keys_in: float) -> float | None:
        total = 0.0
        for source in self.optional:
            outcome = self.best_read(source, keys_in)
            if outcome is None:
                return None
            total += outcome[0].latency
        return total

    def semantic_estimate(self, candidates: float, kind: SemanticPlanKind) -> Estimate | None:
        """Latency and money of the verification work for ``candidates`` rows."""

        options = self.semantic
        assert options is not None
        params = self.params
        pool = candidates * options.unpushed_selectivity
        if kind is SemanticPlanKind.VERIFY_ALL and pool > options.maximum_candidates:
            return None                                  # would exceed the candidate cap at run time
        verified = min(pool, options.page_size / params.expected_semantic_selectivity)
        calls = math.ceil(verified / params.verifier_batch_size) if verified > 0 else 0
        cost = params.verification
        latency = calls * cost.latency_ms_per_call + verified * cost.latency_ms_per_candidate
        money = calls * cost.money_per_call + verified * cost.money_per_candidate
        if kind is SemanticPlanKind.VECTOR_SHORTLIST:
            latency += params.embedding.latency_ms_per_call
            money += params.embedding.money_per_call
        return Estimate(latency, money)

    def ranking_objective(self, estimate: Estimate, kind: SemanticPlanKind | None) -> float:
        """The number plans are compared by: the objective, with the approximate plan
        charged for the margin it must clear to beat the exact one."""

        objective = estimate.objective(self.params)
        if kind is SemanticPlanKind.VECTOR_SHORTLIST:
            objective /= 1.0 - self.params.shortlist_min_saving
        return objective

    # --- evaluating one explicit plan (used by the tests as the oracle) ----------

    def evaluate(
        self,
        order: Sequence[tuple[str, bool]],
        kind: SemanticPlanKind | None = None,
        shortlist: int | None = None,
    ) -> Estimate | None:
        """Estimate of reading the narrowing sources in ``order`` with the given
        restrict flags, then the optional sources, then the semantic step."""

        if sorted(name for name, _ in order) != sorted(s.name for s in self.narrowing):
            raise ValueError("order must name every narrowing source exactly once")
        by_name = {s.name: s for s in self.narrowing}
        latency = 0.0
        read: frozenset[str] = frozenset()
        candidates = None
        for index, (name, restrict) in enumerate(order):
            source = by_name[name]
            keys_in = None if not read else self.keys_after(read)
            last = index == len(order) - 1
            if kind is SemanticPlanKind.VECTOR_SHORTLIST and source is self.anchor:
                if not last or shortlist is None:
                    return None
                ranked = self.ranked_read(keys_in, shortlist)
                if ranked is None:
                    return None
                outcome, pool, rows = ranked
                latency += outcome.latency
                candidates = rows
                recall = self.params.recall_model.expected_recall(float(shortlist), pool)
                if self.semantic is None or recall < self.semantic.required_recall:
                    return None
            else:
                outcome = self.plain_read(source, keys_in, restrict)
                if outcome is None:
                    return None
                latency += outcome.latency
            read = read | {name}
        if candidates is None:
            candidates = self.keys_after(read)
        optional = self.optional_cost(candidates)
        if optional is None:
            return None
        latency += optional
        money = 0.0
        if kind is not None:
            semantic = self.semantic_estimate(candidates, kind)
            if semantic is None:
                return None
            latency += semantic.latency_ms
            money += semantic.money
        return Estimate(latency, money)


def optimize(problem: Problem, *, rule_order: Sequence[str] | None = None, rule_kind: SemanticPlanKind | None = None) -> OptimizerResult | Fallback:
    """Find the cheapest feasible schedule and semantic variant.

    ``rule_order``/``rule_kind`` describe the planner's fixed-rule plan; ties are
    resolved toward it so the optimizer only deviates when strictly cheaper.
    """

    params = problem.params
    narrowing = problem.narrowing
    if len(narrowing) > params.maximum_dp_sources:
        return Fallback(f"{len(narrowing)} result-bounding sources exceed the limit of {params.maximum_dp_sources}")
    n = len(narrowing)
    names = [s.name for s in narrowing]
    full = (1 << n) - 1

    rule_position = {name: i for i, name in enumerate(rule_order or ())}

    def closeness(order) -> tuple[int, ...]:
        """How near an order is to the planner's own (lexicographically smaller is nearer)."""

        return tuple(rule_position.get(name, len(rule_position)) for name, _ in order)

    # DP over subsets of the narrowing sources: cheapest way to have read exactly this set.
    best: dict[int, tuple[float, tuple[tuple[str, bool], ...]]] = {0: (0.0, ())}
    for mask in range(1, full + 1):
        candidate = None
        for r in range(n):
            bit = 1 << r
            if not mask & bit or (mask ^ bit) not in best:
                continue
            previous_cost, previous_order = best[mask ^ bit]
            read = frozenset(names[i] for i in range(n) if (mask ^ bit) & (1 << i))
            keys_in = problem.keys_after(read) if read else None
            outcome = problem.best_read(narrowing[r], keys_in)
            if outcome is None:
                continue
            cost = previous_cost + outcome[0].latency
            order = (*previous_order, (names[r], outcome[1]))
            tolerance = 1e-9 * max(1.0, cost)
            if (
                candidate is None
                or cost < candidate[0] - tolerance
                or (abs(cost - candidate[0]) <= tolerance and closeness(order) < closeness(candidate[1]))
            ):
                candidate = (cost, order)
        if candidate is not None:
            best[mask] = candidate

    considered = 0
    options: list[tuple[float, Estimate, tuple[tuple[str, bool], ...], SemanticDecision | None]] = []

    def consider(scan_latency: float, order, candidates: float, decision: SemanticDecision | None, kind) -> None:
        nonlocal considered
        considered += 1
        optional = problem.optional_cost(candidates)
        if optional is None:
            return
        latency, money = scan_latency + optional, 0.0
        if kind is not None:
            semantic = problem.semantic_estimate(candidates, kind)
            if semantic is None:
                return
            latency += semantic.latency_ms
            money += semantic.money
        estimate = Estimate(latency, money)
        if estimate.within(problem.constraints):
            options.append((problem.ranking_objective(estimate, kind), estimate, order, decision))

    semantic = problem.semantic
    if semantic is None:
        if full in best:
            consider(best[full][0], best[full][1], problem.keys_after(frozenset(names)), None, None)
    else:
        if full in best and semantic.verify_all_allowed:  # Plan A: any order
            consider(
                best[full][0], best[full][1], problem.keys_after(frozenset(names)),
                SemanticDecision(SemanticPlanKind.VERIFY_ALL, None), SemanticPlanKind.VERIFY_ALL,
            )
        anchor_last = full ^ 1  # every narrowing source except the anchor (index 0)
        if semantic.shortlist_allowed and anchor_last in best:
            cost_before, order_before = best[anchor_last]
            read = frozenset(names[1:])
            keys_in = problem.keys_after(read) if read else None
            for size in _shortlist_ladder(semantic):
                ranked = problem.ranked_read(keys_in, size)
                if ranked is None:
                    continue
                outcome, pool, rows = ranked
                if params.recall_model.expected_recall(float(size), pool) < semantic.required_recall:
                    continue
                restricted = bool(problem.required)
                consider(
                    cost_before + outcome.latency,
                    (*order_before, (problem.anchor.name, restricted)),
                    rows,
                    SemanticDecision(SemanticPlanKind.VECTOR_SHORTLIST, size),
                    SemanticPlanKind.VECTOR_SHORTLIST,
                )
    if not options:
        return Fallback("no candidate plan is estimated to satisfy the limits and quality requirement")

    def rank(option):
        objective, _, order, decision = option
        matches_rules = (
            rule_order is not None
            and [name for name, _ in order[: len(rule_order)]] == list(rule_order)
            and (rule_kind is None or (decision is not None and decision.kind is rule_kind))
        )
        # Lowest objective first; on an exact tie keep the planner's own plan.
        return (objective, 0 if matches_rules else 1)

    objective, estimate, order, decision = min(options, key=rank)
    candidates_final = (
        decision is not None and decision.kind is SemanticPlanKind.VECTOR_SHORTLIST and decision.shortlist_size or None
    )
    steps = [AssemblyStep(name, StepRole.ANCHOR if name == problem.anchor.name else StepRole.REQUIRED, restrict) for name, restrict in order]
    keys_final = (
        problem.ranked_read(problem.keys_after(frozenset(names[1:])) if len(names) > 1 else None, candidates_final)[2]
        if candidates_final
        else problem.keys_after(frozenset(names))
    )
    for source in problem.optional:
        outcome = problem.best_read(source, keys_final)
        steps.append(AssemblyStep(source.name, StepRole.OPTIONAL, bool(outcome and outcome[1])))
    return OptimizerResult(tuple(steps), decision, estimate, objective, considered)


def _shortlist_ladder(options: SemanticOptions) -> list[int]:
    """Shortlist sizes to try: the start, doubling, up to and including the cap."""

    sizes: list[int] = []
    size = options.shortlist_start
    while size < options.shortlist_cap:
        sizes.append(size)
        size *= 2
    sizes.append(options.shortlist_cap)
    return sorted(set(sizes))


def brute_force(problem: Problem, *, with_semantic: bool = True) -> tuple[float, Estimate] | None:
    """Reference answer: try every order, every restrict flag and every semantic variant.

    Exponential; used by the tests to check the DP finds the same optimum.
    """

    best: tuple[float, Estimate] | None = None
    names = [s.name for s in problem.narrowing]
    variants: list[tuple[SemanticPlanKind | None, int | None]] = [(None, None)]
    if problem.semantic is not None and with_semantic:
        variants = [(SemanticPlanKind.VERIFY_ALL, None)] if problem.semantic.verify_all_allowed else []
        if problem.semantic.shortlist_allowed:
            variants += [(SemanticPlanKind.VECTOR_SHORTLIST, size) for size in _shortlist_ladder(problem.semantic)]
    for permutation in itertools.permutations(names):
        for flags in itertools.product((True, False), repeat=len(permutation)):
            order = list(zip(permutation, flags))
            for kind, size in variants:
                estimate = problem.evaluate(order, kind, size)
                if estimate is None or not estimate.within(problem.constraints):
                    continue
                objective = problem.ranking_objective(estimate, kind)
                if best is None or objective < best[0]:
                    best = (objective, estimate)
    return best
