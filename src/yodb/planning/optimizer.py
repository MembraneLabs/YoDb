"""Cost-based choice of how to read sources and which extension variant to use.

The planner always has a correct plan from its fixed rules.  This module finds a
*cheaper* one when it has enough statistics, and says nothing otherwise.

What is chosen
--------------
* the order in which the sources that bound the result (the anchor and the
  required contributors) are read, and for each read whether the IDs learned so
  far should restrict it;
* whether enriching (optional) contributors are restricted by the final ID set;
* which *variant* of an extension operator to use.  The optimizer knows nothing
  about what an extension does: it offers :class:`Variant` objects (a cost
  function, and optionally a demand on how the anchor is read) and the search
  picks one.

Why dynamic programming works here
----------------------------------
Under the usual independence assumption the number of IDs known after reading a
*set* of sources does not depend on the order they were read in, so the cheapest
way to have read a set is the cheapest way to have read a smaller set plus the
cost of the last read.  That is a DP over subsets: ``2^m * m`` steps instead of
``m!`` orders.  An extension does not disturb it: its cost depends only on the
final candidate count (order-independent), and a ranked-read demand is only valid
as the *last* narrowing read, which is one extra transition.

Everything is a pure function of :class:`SourceInput` and :class:`CostParameters`,
so a different cost term, recall model or source behavior is a local change, and
the brute-force check in the tests uses the very same cost function.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import itertools
import math

from .contracts import AssemblyStep, StepRole
from .statistics import SourceCostProfile

INFINITY = math.inf


@dataclass(frozen=True)
class CostParameters:
    """Tunable weights and assumptions of the cost model."""

    # Latency-equivalent of one unit of money: objective = latency_ms + this * money.
    money_weight: float = 1_000.0
    # An approximate variant (e.g. a shortlist) must beat the exact one by this
    # fraction to be preferred (its cost is divided by ``1 - margin`` when
    # compared).  Not applied when no exact variant is feasible.
    approximation_min_saving: float = 0.2
    # Fraction of rows kept by filter terms the sources did not enforce.
    unpushed_filter_selectivity: float = 0.5
    maximum_dp_sources: int = 10

    def __post_init__(self) -> None:
        if not (math.isfinite(self.money_weight) and self.money_weight >= 0):
            raise ValueError("money_weight must be a finite non-negative number")
        if not 0.0 < self.unpushed_filter_selectivity <= 1.0:
            raise ValueError("unpushed_filter_selectivity must be within (0, 1]")
        if not 0.0 <= self.approximation_min_saving < 1.0:
            raise ValueError("approximation_min_saving must be within [0, 1)")
        if self.maximum_dp_sources < 1:
            raise ValueError("maximum_dp_sources must be positive")


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
class RankedRead:
    """A demand on the combine step: rank rows and keep only the top ``shortlist``.

    By default the anchor is read *last*, restricted by every required source, and
    does the ranking itself.  With ``via_store`` a separate ranking source (a vector
    store, role SHORTLIST) is read after the required sources and before the anchor,
    which is then read only for the K IDs it returned.  ``ranking_per_row_ms`` is the
    extra cost of ranking each row the read chooses from."""

    shortlist: int
    ranking_per_row_ms: float = 0.0
    via_store: bool = False

    def __post_init__(self) -> None:
        if self.shortlist < 1:
            raise ValueError("shortlist must be positive")
        if not (math.isfinite(self.ranking_per_row_ms) and self.ranking_per_row_ms >= 0):
            raise ValueError("ranking_per_row_ms must be a finite non-negative number")


@dataclass(frozen=True)
class StoreRead:
    """A shortlist read from a separate store, then the anchor read for the IDs it returned."""

    latency: float
    candidates: "Candidates"
    store_restricted: bool
    anchor_restricted: bool


@dataclass(frozen=True)
class Candidates:
    """What flows out of the combine step into an extension operator."""

    count: float   # rows delivered
    pool: float    # rows a ranked read chose from (equal to ``count`` when not ranked)


@dataclass(frozen=True)
class Variant:
    """One legal way to run an extension operator, as the cost model sees it.

    ``cost`` estimates the operator's own work for the candidates it receives,
    or returns None when this variant cannot satisfy its own requirements (a
    candidate cap, a quality bar).  ``demand`` states how the anchor must be
    read for the variant to be valid.  ``payload`` is the operator's own record
    of the decision, handed back untouched in the result.
    """

    name: str
    cost: Callable[[Candidates], Estimate | None]
    demand: RankedRead | None = None
    approximate: bool = False
    payload: object = None


@dataclass(frozen=True)
class OptimizerResult:
    schedule: tuple[AssemblyStep, ...]
    variant: Variant | None
    estimate: Estimate
    objective: float
    candidates_considered: int

    @property
    def decision(self) -> object:
        """The chosen variant's payload (None when the query has no extension)."""

        return None if self.variant is None else self.variant.payload


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
        variants: Sequence[Variant] = (),
        constraints: Constraints = Constraints(),
    ) -> None:
        anchors = [s for s in sources if s.role is StepRole.ANCHOR]
        if len(anchors) != 1 or len({s.name for s in sources}) != len(sources):
            raise ValueError("exactly one anchor and unique source names are required")
        self.params = params
        self.transfer_bound = maximum_transfer_keys
        self.variants = tuple(variants)
        self.constraints = constraints
        self.anchor = anchors[0]
        self.required = [s for s in sources if s.role is StepRole.REQUIRED]
        self.optional = [s for s in sources if s.role is StepRole.OPTIONAL]
        stores = [s for s in sources if s.role is StepRole.SHORTLIST]
        if len(stores) > 1:
            raise ValueError("at most one shortlist source is supported")
        self.store = stores[0] if stores else None
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

    def ranked_read(self, keys_in: float | None, demand: RankedRead) -> tuple[_Read, Candidates] | None:
        """Cost of the anchor read a :class:`RankedRead` demands, and what it delivers."""

        anchor = self.anchor
        restrict = bool(self.required)
        if restrict:
            bound = self.bound(anchor)
            if keys_in is None or bound is None or keys_in > bound:
                return None            # a ranked read cut before the required matches is unsafe
            pool = keys_in * self.pass_fraction(anchor)
        else:
            pool = anchor.filtered_rows
        rows = min(float(demand.shortlist), pool)
        profile = anchor.profile
        latency = (
            profile.call_latency_ms
            + (profile.per_key_latency_ms * keys_in if restrict and keys_in else 0.0)
            + profile.per_row_latency_ms * rows
            + demand.ranking_per_row_ms * pool
        )
        return _Read(latency, rows), Candidates(rows, pool)

    def store_read(self, keys_in: float | None, demand: RankedRead) -> StoreRead | None:
        """Cost of ranking in the store (after the required sources), then reading the anchor
        for the IDs it returned.  None when this problem has no store or the anchor cannot be read."""

        store = self.store
        if store is None:
            return None
        bound = self.bound(store)
        restricted = keys_in is not None and bound is not None and keys_in <= bound
        coverage = min(1.0, store.total_rows / self.universe) if self.universe > 0 else 0.0
        pool = keys_in * coverage if restricted else store.total_rows
        rows = min(float(demand.shortlist), pool)
        profile = store.profile
        latency = (
            profile.call_latency_ms
            + (profile.per_key_latency_ms * keys_in if restricted and keys_in else 0.0)
            + profile.per_row_latency_ms * rows
            + demand.ranking_per_row_ms * pool
        )
        if not restricted:
            # the K nearest of the whole store still have to pass every required source
            for source in self.required:
                rows *= self.pass_fraction(source)
        outcome = self.best_read(self.anchor, rows)
        if outcome is None:
            return None
        read, anchor_restricted = outcome
        return StoreRead(latency + read.latency, Candidates(read.rows, pool), restricted, anchor_restricted)

    def optional_cost(self, keys_in: float) -> float | None:
        total = 0.0
        for source in self.optional:
            outcome = self.best_read(source, keys_in)
            if outcome is None:
                return None
            total += outcome[0].latency
        return total

    def ranking_objective(self, estimate: Estimate, variant: Variant | None) -> float:
        """The number plans are compared by: the objective, with an approximate
        variant charged for the margin it must clear to beat an exact one."""

        objective = estimate.objective(self.params)
        if variant is not None and variant.approximate:
            objective /= 1.0 - self.params.approximation_min_saving
        return objective

    def finish(self, scan_latency: float, candidates: Candidates, variant: Variant | None) -> Estimate | None:
        """Add the enrichers' reads and the variant's own work to the reading cost."""

        optional = self.optional_cost(candidates.count)
        if optional is None:
            return None
        latency, money = scan_latency + optional, 0.0
        if variant is not None:
            own = variant.cost(candidates)
            if own is None:
                return None
            latency += own.latency_ms
            money += own.money
        return Estimate(latency, money)

    def _evaluate_via_store(self, order: Sequence[tuple[str, bool]], variant: Variant) -> Estimate | None:
        """The oracle for a store-ranked variant: required sources, then the store, then the anchor."""

        if order[-1][0] != self.anchor.name:
            return None                       # the anchor is read last, for the shortlisted IDs
        by_name = {s.name: s for s in self.narrowing}
        latency = 0.0
        read: frozenset[str] = frozenset()
        for name, restrict in order[:-1]:
            keys_in = None if not read else self.keys_after(read)
            outcome = self.plain_read(by_name[name], keys_in, restrict)
            if outcome is None:
                return None
            latency += outcome.latency
            read = read | {name}
        keys_in = self.keys_after(read) if read else None
        result = self.store_read(keys_in, variant.demand)
        if result is None:
            return None
        return self.finish(latency + result.latency, result.candidates, variant)

    # --- evaluating one explicit plan (used by the tests as the oracle) ----------

    def evaluate(self, order: Sequence[tuple[str, bool]], variant: Variant | None = None) -> Estimate | None:
        """Estimate of reading the narrowing sources in ``order`` with the given
        restrict flags, then the enrichers, then the variant's own work."""

        if sorted(name for name, _ in order) != sorted(s.name for s in self.narrowing):
            raise ValueError("order must name every narrowing source exactly once")
        if variant is not None and variant.demand is not None and variant.demand.via_store:
            return self._evaluate_via_store(order, variant)
        by_name = {s.name: s for s in self.narrowing}
        latency = 0.0
        read: frozenset[str] = frozenset()
        candidates: Candidates | None = None
        for index, (name, restrict) in enumerate(order):
            source = by_name[name]
            keys_in = None if not read else self.keys_after(read)
            if variant is not None and variant.demand is not None and source is self.anchor:
                if index != len(order) - 1:
                    return None
                ranked = self.ranked_read(keys_in, variant.demand)
                if ranked is None:
                    return None
                latency += ranked[0].latency
                candidates = ranked[1]
            else:
                outcome = self.plain_read(source, keys_in, restrict)
                if outcome is None:
                    return None
                latency += outcome.latency
            read = read | {name}
        if candidates is None:
            final = self.keys_after(read)
            candidates = Candidates(final, final)
        return self.finish(latency, candidates, variant)


def optimize(
    problem: Problem,
    *,
    rule_order: Sequence[str] | None = None,
    rule_variant: Variant | None = None,
) -> OptimizerResult | Fallback:
    """Find the cheapest feasible schedule and variant.

    ``rule_order``/``rule_variant`` describe the planner's fixed-rule plan; ties
    are resolved toward it so the optimizer only deviates when strictly cheaper.
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
    options: list[tuple[float, Estimate, tuple[tuple[str, bool], ...], Variant | None, Candidates]] = []

    def consider(scan_latency: float, order, candidates: Candidates, variant: Variant | None) -> None:
        nonlocal considered
        considered += 1
        estimate = problem.finish(scan_latency, candidates, variant)
        if estimate is not None and estimate.within(problem.constraints):
            options.append((problem.ranking_objective(estimate, variant), estimate, order, variant, candidates))

    anchor_last = full ^ 1  # every narrowing source except the anchor (index 0)
    for variant in problem.variants or (None,):
        demand = None if variant is None else variant.demand
        if demand is not None and demand.via_store:
            if problem.store is not None and anchor_last in best:
                cost_before, order_before = best[anchor_last]
                keys_in = problem.keys_after(frozenset(names[1:])) if len(names) > 1 else None
                outcome = problem.store_read(keys_in, demand)
                if outcome is not None:
                    consider(
                        cost_before + outcome.latency,
                        (*order_before, (problem.store.name, outcome.store_restricted), (problem.anchor.name, outcome.anchor_restricted)),
                        outcome.candidates,
                        variant,
                    )
            continue
        if demand is None:
            if full in best:
                final = problem.keys_after(frozenset(names))
                consider(best[full][0], best[full][1], Candidates(final, final), variant)
        elif anchor_last in best:
            cost_before, order_before = best[anchor_last]
            keys_in = problem.keys_after(frozenset(names[1:])) if len(names) > 1 else None
            ranked = problem.ranked_read(keys_in, demand)
            if ranked is not None:
                consider(
                    cost_before + ranked[0].latency,
                    (*order_before, (problem.anchor.name, bool(problem.required))),
                    ranked[1],
                    variant,
                )
    if not options:
        return Fallback("no candidate plan is estimated to satisfy the limits and quality requirement")

    def rank(option):
        objective, _, order, variant, _ = option
        matches_rules = (
            rule_order is not None
            and [name for name, _ in order[: len(rule_order)]] == list(rule_order)
            and (rule_variant is None or variant is rule_variant)
        )
        # Lowest objective first; on an exact tie keep the planner's own plan.
        return (objective, 0 if matches_rules else 1)

    objective, estimate, order, variant, candidates = min(options, key=rank)
    def role_of(name: str) -> StepRole:
        if name == problem.anchor.name:
            return StepRole.ANCHOR
        return StepRole.SHORTLIST if problem.store is not None and name == problem.store.name else StepRole.REQUIRED

    steps = [AssemblyStep(name, role_of(name), restrict) for name, restrict in order]
    for source in problem.optional:
        outcome = problem.best_read(source, candidates.count)
        steps.append(AssemblyStep(source.name, StepRole.OPTIONAL, bool(outcome and outcome[1])))
    return OptimizerResult(tuple(steps), variant, estimate, objective, considered)


def brute_force(problem: Problem) -> tuple[float, Estimate] | None:
    """Reference answer: try every order, every restrict flag and every variant.

    Exponential; used by the tests to check the DP finds the same optimum.
    """

    best: tuple[float, Estimate] | None = None
    names = [s.name for s in problem.narrowing]
    for permutation in itertools.permutations(names):
        for flags in itertools.product((True, False), repeat=len(permutation)):
            order = list(zip(permutation, flags))
            for variant in problem.variants or (None,):
                estimate = problem.evaluate(order, variant)
                if estimate is None or not estimate.within(problem.constraints):
                    continue
                objective = problem.ranking_objective(estimate, variant)
                if best is None or objective < best[0]:
                    best = (objective, estimate)
    return best
