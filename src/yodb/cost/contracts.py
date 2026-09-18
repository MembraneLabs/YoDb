"""Portable cost-estimation values; estimates are never execution authority."""

from __future__ import annotations

from dataclasses import dataclass
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
    estimated_row_bytes: float
    estimated_transfer_bytes: float
    estimated_memory_bytes: float
    estimated_backend_work: float
    estimated_latency_ms: float
    confidence: CostConfidence
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanAssessment:
    estimate: CostEstimate
    stages: tuple[tuple[str, CostEstimate], ...]


@runtime_checkable
class SourceCostEstimator(Protocol):
    @property
    def source_kind(self) -> SourceKind: ...

    def estimate_scan(self, scan: SourceScanPlan, inspection: SourceInspection) -> CostEstimate: ...
