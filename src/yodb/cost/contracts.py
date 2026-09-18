"""Portable estimates, policy inputs, and adapter contracts for federation.

The values here intentionally describe effects that can be compared across
sources: records, bytes, calls, coordinator memory, and elapsed time. Native
engine cost units remain optional adapter diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping, Protocol, runtime_checkable

from ..catalog import SourceKind
from ..planning import CandidatePlan, KeyTransferPlan, SourceScanPlan
from ..runtime import CatalogEvaluation


class EstimateConfidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ResultCompleteness(str, Enum):
    EXACT = "exact"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RangeEstimate:
    """Expected and conservative upper values in one portable unit."""

    expected: float
    upper_bound: float

    def __post_init__(self) -> None:
        if self.expected < 0 or self.upper_bound < 0:
            raise ValueError("estimate values must be non-negative")
        if self.upper_bound < self.expected:
            raise ValueError("an estimate upper bound must be at least its expected value")


@dataclass(frozen=True)
class ResultEstimate:
    """Predicted result shape flowing out of an operator."""

    rows: RangeEstimate
    row_bytes: RangeEstimate
    completeness: ResultCompleteness = ResultCompleteness.EXACT

    @property
    def output_bytes(self) -> RangeEstimate:
        return RangeEstimate(
            expected=self.rows.expected * self.row_bytes.expected,
            upper_bound=self.rows.upper_bound * self.row_bytes.upper_bound,
        )


@dataclass(frozen=True)
class RemoteOperationEstimate:
    """An adapter's estimate for one native remote request.

    ``source_details`` may contain a source-local work score or access-path
    explanation. The federated chooser never compares those values across
    source kinds.
    """

    result: ResultEstimate
    remote_calls: RangeEstimate
    startup_latency_ms: RangeEstimate | None
    execution_latency_ms: RangeEstimate | None
    confidence: EstimateConfidence
    statistics_collected_at: datetime | None
    provenance: tuple[str, ...]
    assumptions: tuple[str, ...] = ()
    source_details: Mapping[str, float | str | bool] | None = None


@dataclass(frozen=True)
class FederatedEstimate:
    """A source-neutral estimate for one federated DAG node or full plan."""

    result: ResultEstimate
    transfer_bytes: RangeEstimate
    remote_calls: RangeEstimate
    critical_path_latency_ms: RangeEstimate | None
    coordinator_memory_bytes: RangeEstimate
    coordinator_cpu_records: RangeEstimate
    confidence: EstimateConfidence
    provenance: tuple[str, ...]
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanAssessment:
    """Full estimate plus explainable estimates for every physical stage."""

    candidate: CandidatePlan
    estimate: FederatedEstimate
    stages: tuple[tuple[str, FederatedEstimate], ...]


@dataclass(frozen=True)
class FederatedQueryBudget:
    """Hard deployment/query limits evaluated against conservative bounds."""

    maximum_remote_calls: int | None = None
    maximum_key_count: int | None = None
    maximum_key_bytes: int | None = None
    maximum_intermediate_rows: int | None = None
    maximum_transfer_bytes: int | None = None
    maximum_coordinator_memory_bytes: int | None = None
    maximum_latency_ms: int | None = None
    require_exact_results: bool = True
    minimum_confidence_for_large_work: EstimateConfidence = EstimateConfidence.LOW


@dataclass(frozen=True)
class PlanRejection:
    """One policy reason a candidate cannot execute."""

    code: str
    message: str
    stage: str | None = None


@dataclass(frozen=True)
class PlanDecision:
    """The admitted winner or every rejection when no plan is safe."""

    selected: PlanAssessment | None
    rejected: tuple[tuple[PlanAssessment, tuple[PlanRejection, ...]], ...]


@runtime_checkable
class SourceCostEstimator(Protocol):
    """Adapter-owned estimation for a declared source kind."""

    @property
    def source_kind(self) -> SourceKind: ...

    def estimate_remote_scan(
        self,
        scan: SourceScanPlan,
        active: CatalogEvaluation,
    ) -> RemoteOperationEstimate: ...

    def estimate_key_lookup(
        self,
        transfer: KeyTransferPlan,
        key_result: ResultEstimate,
        active: CatalogEvaluation,
    ) -> RemoteOperationEstimate: ...

