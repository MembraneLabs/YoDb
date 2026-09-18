"""Hard federated query-budget checks and deterministic candidate selection."""

from __future__ import annotations

from math import inf

from .contracts import (
    EstimateConfidence,
    FederatedQueryBudget,
    PlanAssessment,
    PlanDecision,
    PlanRejection,
    ResultCompleteness,
)
from ..planning import InMemoryAssemblyPlan, KeyTransferPlan


class FederatedCostPolicy:
    """Reject unsafe candidates, then select deterministically among survivors."""

    def decide(
        self,
        assessments: tuple[PlanAssessment, ...],
        budget: FederatedQueryBudget,
    ) -> PlanDecision:
        admitted: list[PlanAssessment] = []
        rejected: list[tuple[PlanAssessment, tuple[PlanRejection, ...]]] = []
        for assessment in assessments:
            reasons = self.evaluate(assessment, budget)
            if reasons:
                rejected.append((assessment, reasons))
            else:
                admitted.append(assessment)
        return PlanDecision(
            selected=min(admitted, key=_selection_key) if admitted else None,
            rejected=tuple(rejected),
        )

    def evaluate(
        self,
        assessment: PlanAssessment,
        budget: FederatedQueryBudget,
    ) -> tuple[PlanRejection, ...]:
        estimate = assessment.estimate
        reasons: list[PlanRejection] = []
        _check(reasons, budget.maximum_remote_calls, estimate.remote_calls.upper_bound, "MAXIMUM_REMOTE_CALLS", "remote calls")
        intermediate_rows = max(estimate.result.rows.upper_bound, estimate.coordinator_cpu_records.upper_bound)
        _check(reasons, budget.maximum_intermediate_rows, intermediate_rows, "MAXIMUM_INTERMEDIATE_ROWS", "intermediate rows")
        _check(reasons, budget.maximum_transfer_bytes, estimate.transfer_bytes.upper_bound, "MAXIMUM_TRANSFER_BYTES", "transfer bytes")
        _check(reasons, budget.maximum_coordinator_memory_bytes, estimate.coordinator_memory_bytes.upper_bound, "MAXIMUM_COORDINATOR_MEMORY", "coordinator memory bytes")
        if estimate.critical_path_latency_ms is not None:
            _check(reasons, budget.maximum_latency_ms, estimate.critical_path_latency_ms.upper_bound, "MAXIMUM_LATENCY", "critical-path latency ms")
        if budget.require_exact_results and estimate.result.completeness is not ResultCompleteness.EXACT:
            reasons.append(PlanRejection("RESULT_COMPLETENESS", "The query requires exact results but this candidate is not exact."))
        if _is_large_work(intermediate_rows, budget) and _confidence_rank(estimate.confidence) < _confidence_rank(budget.minimum_confidence_for_large_work):
            reasons.append(
                PlanRejection(
                    "INSUFFICIENT_ESTIMATE_CONFIDENCE",
                    f"Large work requires at least {budget.minimum_confidence_for_large_work.value} estimate confidence.",
                )
            )
        if budget.maximum_key_count is not None or budget.maximum_key_bytes is not None:
            for stage_name, stage in assessment.stages:
                if not stage_name.startswith("coordinator:key_set:"):
                    continue
                _check(reasons, budget.maximum_key_count, stage.result.rows.upper_bound, "MAXIMUM_KEY_COUNT", "transferred logical ids", stage_name)
                _check(reasons, budget.maximum_key_bytes, stage.result.output_bytes.upper_bound, "MAXIMUM_KEY_BYTES", "transferred logical-id bytes", stage_name)
        for transfer in _key_transfers(assessment):
            key_upper = _stage_value(assessment, f"coordinator:key_set:{transfer.keys.input.source.source_name}", "rows")
            if key_upper is not None and key_upper > transfer.keys.maximum_distinct_keys:
                reasons.append(
                    PlanRejection(
                        "KEY_SET_PLAN_LIMIT",
                        f"Estimated upper logical IDs {key_upper:.0f} exceeds the plan key-set limit {transfer.keys.maximum_distinct_keys}.",
                        f"coordinator:key_set:{transfer.keys.input.source.source_name}",
                    )
                )
        return tuple(reasons)


def _check(
    reasons: list[PlanRejection],
    limit: int | None,
    actual: float,
    code: str,
    label: str,
    stage: str | None = None,
) -> None:
    if limit is not None and actual > limit:
        reasons.append(PlanRejection(code, f"Estimated upper {label} {actual:.0f} exceeds budget {limit}.", stage))


def _is_large_work(upper_rows: float, budget: FederatedQueryBudget) -> bool:
    # A configured intermediate-row budget defines what is operationally
    # material. Without one, confidence affects explanation but not admission.
    return budget.maximum_intermediate_rows is not None and upper_rows > budget.maximum_intermediate_rows / 2


def _selection_key(assessment: PlanAssessment) -> tuple[float, float, float, float, int, str]:
    estimate = assessment.estimate
    latency = estimate.critical_path_latency_ms.expected if estimate.critical_path_latency_ms is not None else inf
    return (
        latency,
        estimate.transfer_bytes.expected,
        estimate.coordinator_memory_bytes.expected,
        estimate.remote_calls.expected,
        -_confidence_rank(estimate.confidence),
        assessment.candidate.strategy,
    )


def _confidence_rank(confidence: EstimateConfidence) -> int:
    return {EstimateConfidence.LOW: 0, EstimateConfidence.MEDIUM: 1, EstimateConfidence.HIGH: 2}[confidence]


def _key_transfers(assessment: PlanAssessment) -> tuple[KeyTransferPlan, ...]:
    plan = assessment.candidate.plan
    if not isinstance(plan, InMemoryAssemblyPlan):
        return ()
    return tuple(item for item in plan.contributors if isinstance(item, KeyTransferPlan))


def _stage_value(assessment: PlanAssessment, stage_name: str, value: str) -> float | None:
    for name, estimate in assessment.stages:
        if name == stage_name:
            return estimate.result.rows.upper_bound if value == "rows" else None
    return None
