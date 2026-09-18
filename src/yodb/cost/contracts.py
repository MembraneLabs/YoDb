"""Portable cost-estimation values; estimates are never execution authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable

from ..catalog import SourceKind
from ..inspection import SourceInspection
from ..planning import SourceScanPlan


class CostConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class CostEstimate:
    estimated_rows: float
    upper_bound_rows: float
    estimated_row_bytes: float
    estimated_transfer_bytes: float
    upper_bound_transfer_bytes: float
    estimated_memory_bytes: float
    upper_bound_memory_bytes: float
    estimated_backend_work: float
    estimated_latency_ms: float
    confidence: CostConfidence
    statistics_collected_at: datetime | None
    provenance: tuple[str, ...]
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanAssessment:
    estimate: CostEstimate
    stages: tuple[tuple[str, CostEstimate], ...]


@dataclass(frozen=True)
class PlanComparison:
    """Baseline assessment plus safe-but-not-yet-executed candidate estimates."""

    baseline: PlanAssessment
    candidates: tuple[tuple[str, PlanAssessment], ...]


@runtime_checkable
class SourceCostEstimator(Protocol):
    @property
    def source_kind(self) -> SourceKind: ...

    def estimate_scan(self, scan: SourceScanPlan, inspection: SourceInspection) -> CostEstimate: ...
