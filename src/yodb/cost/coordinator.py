"""Compose backend scan estimates into estimates for current physical plans."""

from __future__ import annotations

from ..planning import InMemoryAssemblyPlan, PhysicalQueryPlan, SingleSourcePlan
from ..runtime import CatalogEvaluation
from .contracts import CostConfidence, CostEstimate, PlanAssessment
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
        assembly = CostEstimate(anchor_rows, 0.0, transfer, memory, sum(cost.estimated_rows for cost in scan_costs), sum(cost.estimated_latency_ms for cost in scan_costs) + anchor_rows / 100_000, CostConfidence.LOW, ("unoptimized sequential in-memory assembly",))
        return PlanAssessment(assembly, (*estimates, ("in_memory_assembly", assembly)))
