"""Estimate federated physical DAGs using portable adapter outputs."""

from __future__ import annotations

from ..planning import (
    CandidatePlan,
    InMemoryAssemblyPlan,
    KeySetPlan,
    KeyTransferPlan,
    SingleSourcePlan,
    SourceScanPlan,
)
from ..runtime import CatalogEvaluation
from .contracts import (
    EstimateConfidence,
    FederatedEstimate,
    PlanAssessment,
    RangeEstimate,
    RemoteOperationEstimate,
    ResultEstimate,
)
from .registry import CostEstimatorRegistry


class FederatedPlanEstimator:
    """Compose remote estimates into transfer, memory, and latency estimates.

    It does not choose a plan and does not execute remote work.  Independent
    source scans are treated as parallel-eligible for latency estimation; the
    executor later applies connection and source concurrency limits.
    """

    def __init__(self, adapters: CostEstimatorRegistry) -> None:
        self._adapters = adapters

    def assess(self, candidate: CandidatePlan, active: CatalogEvaluation) -> PlanAssessment:
        stages: list[tuple[str, FederatedEstimate]] = []
        scan_cache: dict[int, FederatedEstimate] = {}
        estimate = self._estimate_plan(candidate.plan, active, stages, scan_cache)
        return PlanAssessment(candidate=candidate, estimate=estimate, stages=tuple(stages))

    def assess_all(
        self,
        candidates: tuple[CandidatePlan, ...],
        active: CatalogEvaluation,
    ) -> tuple[PlanAssessment, ...]:
        return tuple(self.assess(candidate, active) for candidate in candidates)

    def _estimate_plan(
        self,
        plan: SingleSourcePlan | InMemoryAssemblyPlan,
        active: CatalogEvaluation,
        stages: list[tuple[str, FederatedEstimate]],
        scan_cache: dict[int, FederatedEstimate],
    ) -> FederatedEstimate:
        if isinstance(plan, SingleSourcePlan):
            remote = self._estimate_scan(plan.scan, active, stages, scan_cache)
            if plan.residual_where is None:
                return remote
            residual = FederatedEstimate(
                result=remote.result,
                transfer_bytes=remote.transfer_bytes,
                remote_calls=remote.remote_calls,
                critical_path_latency_ms=remote.critical_path_latency_ms,
                coordinator_memory_bytes=RangeEstimate(0, 0),
                coordinator_cpu_records=remote.result.rows,
                confidence=EstimateConfidence.LOW,
                provenance=("yodb:coordinator_filter",),
                assumptions=("residual filter selectivity is not assumed to reduce remote transfer",),
            )
            stages.append(("coordinator:residual_filter", residual))
            return residual

        anchor = self._estimate_scan(plan.anchor, active, stages, scan_cache)
        contributors = tuple(
            self._estimate_contributor(contributor, active, stages, scan_cache) for contributor in plan.contributors
        )
        all_inputs = (anchor, *contributors)
        transfer = _sum_ranges(*(item.transfer_bytes for item in all_inputs))
        remote_calls = _sum_ranges(*(item.remote_calls for item in all_inputs))
        input_rows = _sum_ranges(*(item.result.rows for item in all_inputs))
        input_bytes = _sum_ranges(*(item.result.output_bytes for item in all_inputs))
        # Assembly holds incoming rows plus the anchored output during merging.
        # This is intentionally conservative until streaming assembly has a
        # proven lifecycle model.
        memory = RangeEstimate(input_bytes.expected + anchor.result.output_bytes.expected, input_bytes.upper_bound + anchor.result.output_bytes.upper_bound)
        latency = _parallel_latency(*(item.critical_path_latency_ms for item in all_inputs))
        if latency is not None:
            latency = RangeEstimate(latency.expected + input_rows.expected / 100_000, latency.upper_bound + input_rows.upper_bound / 100_000)
        estimate = FederatedEstimate(
            result=anchor.result,
            transfer_bytes=transfer,
            remote_calls=remote_calls,
            critical_path_latency_ms=latency,
            coordinator_memory_bytes=memory,
            coordinator_cpu_records=input_rows,
            confidence=_lowest_confidence(*(item.confidence for item in all_inputs)),
            provenance=("yodb:record_assembly",),
            assumptions=(
                "contributors are left-enriched by declared logical id",
                "global residual filtering, ordering, and paging remain coordinator work",
            ),
        )
        stages.append(("coordinator:record_assembly", estimate))
        return estimate

    def _estimate_contributor(
        self,
        contributor: SourceScanPlan | KeyTransferPlan,
        active: CatalogEvaluation,
        stages: list[tuple[str, FederatedEstimate]],
        scan_cache: dict[int, FederatedEstimate],
    ) -> FederatedEstimate:
        if isinstance(contributor, SourceScanPlan):
            return self._estimate_scan(contributor, active, stages, scan_cache)
        return self._estimate_key_transfer(contributor, active, stages, scan_cache)

    def _estimate_scan(
        self,
        scan: SourceScanPlan,
        active: CatalogEvaluation,
        stages: list[tuple[str, FederatedEstimate]],
        scan_cache: dict[int, FederatedEstimate],
    ) -> FederatedEstimate:
        cached = scan_cache.get(id(scan))
        if cached is not None:
            return cached
        remote = self._adapters.adapter_for(scan.source.source_kind).estimate_remote_scan(scan, active)
        estimate = _from_remote(remote)
        stages.append((f"remote_scan:{scan.source.source_name}", estimate))
        scan_cache[id(scan)] = estimate
        return estimate

    def _estimate_key_transfer(
        self,
        transfer: KeyTransferPlan,
        active: CatalogEvaluation,
        stages: list[tuple[str, FederatedEstimate]],
        scan_cache: dict[int, FederatedEstimate],
    ) -> FederatedEstimate:
        keys = self._estimate_key_set(transfer.keys, active, stages, scan_cache)
        remote = self._adapters.adapter_for(transfer.destination.source.source_kind).estimate_key_lookup(
            transfer,
            keys.result,
            active,
        )
        destination = _from_remote(remote)
        stages.append((f"remote_key_lookup:{transfer.destination.source.source_name}", destination))
        key_bytes = keys.result.output_bytes
        latency = _sequential_latency(keys.critical_path_latency_ms, destination.critical_path_latency_ms)
        estimate = FederatedEstimate(
            result=destination.result,
            # The upstream scan is shared with the assembly anchor. This node
            # adds only the key payload and destination remote work.
            transfer_bytes=_sum_ranges(key_bytes, destination.transfer_bytes),
            remote_calls=destination.remote_calls,
            critical_path_latency_ms=latency,
            coordinator_memory_bytes=keys.coordinator_memory_bytes,
            coordinator_cpu_records=_sum_ranges(keys.coordinator_cpu_records, destination.coordinator_cpu_records),
            confidence=_lowest_confidence(keys.confidence, destination.confidence),
            provenance=("yodb:key_transfer",),
            assumptions=(f"key lookup batches contain at most {transfer.maximum_keys_per_batch} ids",),
        )
        stages.append((f"coordinator:key_transfer:{transfer.link.from_source}->{transfer.link.to_source}", estimate))
        return estimate

    def _estimate_key_set(
        self,
        keys: KeySetPlan,
        active: CatalogEvaluation,
        stages: list[tuple[str, FederatedEstimate]],
        scan_cache: dict[int, FederatedEstimate],
    ) -> FederatedEstimate:
        input_estimate = self._estimate_scan(keys.input, active, stages, scan_cache)
        id_width = _logical_id_width(keys.logical_id.field.spec.type.value)
        rows = RangeEstimate(
            min(input_estimate.result.rows.expected, keys.maximum_distinct_keys),
            input_estimate.result.rows.upper_bound,
        )
        result = ResultEstimate(rows=rows, row_bytes=RangeEstimate(id_width, id_width))
        estimate = FederatedEstimate(
            result=result,
            transfer_bytes=RangeEstimate(0, 0),
            remote_calls=RangeEstimate(0, 0),
            critical_path_latency_ms=input_estimate.critical_path_latency_ms,
            coordinator_memory_bytes=result.output_bytes,
            coordinator_cpu_records=input_estimate.result.rows,
            confidence=input_estimate.confidence,
            provenance=("yodb:key_set",),
            assumptions=("logical ids are deduplicated before transfer",),
        )
        stages.append((f"coordinator:key_set:{keys.input.source.source_name}", estimate))
        return estimate


def _from_remote(remote: RemoteOperationEstimate) -> FederatedEstimate:
    latency = _sum_optional_ranges(remote.startup_latency_ms, remote.execution_latency_ms)
    return FederatedEstimate(
        result=remote.result,
        transfer_bytes=remote.result.output_bytes,
        remote_calls=remote.remote_calls,
        critical_path_latency_ms=latency,
        coordinator_memory_bytes=RangeEstimate(0, 0),
        coordinator_cpu_records=RangeEstimate(0, 0),
        confidence=remote.confidence,
        provenance=remote.provenance,
        assumptions=remote.assumptions,
    )


def _sum_ranges(*ranges: RangeEstimate) -> RangeEstimate:
    return RangeEstimate(sum(item.expected for item in ranges), sum(item.upper_bound for item in ranges))


def _sum_optional_ranges(*ranges: RangeEstimate | None) -> RangeEstimate | None:
    present = tuple(item for item in ranges if item is not None)
    return _sum_ranges(*present) if present else None


def _parallel_latency(*ranges: RangeEstimate | None) -> RangeEstimate | None:
    present = tuple(item for item in ranges if item is not None)
    return RangeEstimate(max(item.expected for item in present), max(item.upper_bound for item in present)) if present else None


def _sequential_latency(*ranges: RangeEstimate | None) -> RangeEstimate | None:
    return _sum_optional_ranges(*ranges)


def _lowest_confidence(*confidences: EstimateConfidence) -> EstimateConfidence:
    rank = {EstimateConfidence.LOW: 0, EstimateConfidence.MEDIUM: 1, EstimateConfidence.HIGH: 2}
    return min(confidences, key=rank.__getitem__)


def _logical_id_width(logical_type: str) -> float:
    return 16.0 if logical_type == "uuid" else 48.0
