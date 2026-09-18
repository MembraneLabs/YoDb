"""Compose backend scan estimates into estimates for current physical plans."""

from __future__ import annotations

from ..planning import InMemoryAssemblyPlan, PhysicalQueryPlan, SingleSourcePlan, SourceScanPlan
from ..query.models import BoundAllExpression, BoundFilterExpression, BoundPredicate
from ..runtime import CatalogEvaluation
from .contracts import CostConfidence, CostEstimate, PlanAssessment, PlanComparison
from .registry import CostEstimatorRegistry


class PlanCostEstimator:
    def __init__(self, adapters: CostEstimatorRegistry) -> None:
        self._adapters = adapters

    def assess(self, plan: PhysicalQueryPlan, active: CatalogEvaluation) -> PlanAssessment:
        scans = (plan.scan,) if isinstance(plan, SingleSourcePlan) else plan.scans
        estimates = []
        for scan in scans:
            inspection = active.sources[scan.source.source_name].inspection
            assert inspection is not None
            estimate = self._adapters.adapter_for(scan.source.source_kind).estimate_scan(scan, inspection)
            estimates.append((f"scan:{scan.source.source_name}", estimate))
        if isinstance(plan, SingleSourcePlan):
            return PlanAssessment(estimates[0][1], tuple(estimates))
        scan_costs = [estimate for _, estimate in estimates]
        anchor_rows = scan_costs[0].estimated_rows
        transfer = sum(cost.estimated_transfer_bytes for cost in scan_costs)
        memory = sum(cost.estimated_transfer_bytes for cost in scan_costs) * 2.0
        upper_transfer = sum(cost.upper_bound_transfer_bytes for cost in scan_costs)
        assembly = CostEstimate(
            anchor_rows, scan_costs[0].upper_bound_rows, 0.0, transfer, upper_transfer,
            memory, upper_transfer * 2.0, sum(cost.estimated_backend_work for cost in scan_costs),
            sum(cost.estimated_latency_ms for cost in scan_costs) + anchor_rows / 100_000,
            CostConfidence.LOW, active.evaluated_at, ("yodb:in_memory_assembly",),
            ("unoptimized sequential in-memory assembly",),
        )
        return PlanAssessment(assembly, (*estimates, ("in_memory_assembly", assembly)))

    def compare(self, plan: PhysicalQueryPlan, active: CatalogEvaluation) -> PlanComparison:
        """Assess the baseline and one semantics-safe candidate without selecting it."""

        baseline = self.assess(plan, active)
        candidate = _conjunctive_pushdown_candidate(plan)
        candidates = () if candidate is None else (("source_local_conjunctive_pushdown", self.assess(candidate, active)),)
        return PlanComparison(baseline=baseline, candidates=candidates)


def _conjunctive_pushdown_candidate(plan: PhysicalQueryPlan) -> InMemoryAssemblyPlan | None:
    if not isinstance(plan, InMemoryAssemblyPlan):
        return None
    predicates = _conjunctive_predicates(plan.query.where)
    if predicates is None:
        return None
    by_source: dict[str, list[BoundPredicate]] = {scan.source.source_name: [] for scan in plan.scans}
    for predicate in predicates:
        matches = [scan for scan in plan.scans if any(field.field.name == predicate.field.name for field in scan.source.fields)]
        if len(matches) != 1:
            return None
        by_source[matches[0].source.source_name].append(predicate)
    if not any(by_source.values()):
        return None
    scans = []
    for scan in plan.scans:
        pushed = tuple(by_source[scan.source.source_name])
        scans.append(SourceScanPlan(scan.source, scan.projection, _all(pushed) if pushed else None, scan.order_by, scan.limit))
    return InMemoryAssemblyPlan(scans=tuple(scans), query=plan.query, maximum_rows_per_source=plan.maximum_rows_per_source)


def _conjunctive_predicates(expression: BoundFilterExpression | None) -> tuple[BoundPredicate, ...] | None:
    if expression is None:
        return ()
    if isinstance(expression, BoundPredicate):
        return (expression,)
    if isinstance(expression, BoundAllExpression):
        children = tuple(_conjunctive_predicates(child) for child in expression.expressions)
        if any(child is None for child in children):
            return None
        return tuple(predicate for child in children for predicate in child or ())
    return None


def _all(predicates: tuple[BoundPredicate, ...]) -> BoundFilterExpression:
    return predicates[0] if len(predicates) == 1 else BoundAllExpression(predicates)
