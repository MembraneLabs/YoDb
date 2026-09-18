"""Federated physical-plan contracts and bounded candidate planning."""

from .contracts import (
    CandidatePlan,
    FederatedContributor,
    InMemoryAssemblyPlan,
    KeySetPlan,
    KeyTransferPlan,
    PhysicalQueryPlan,
    SingleSourcePlan,
    SourceCapabilities,
    SourceCapabilityProvider,
    SourceScanPlan,
)
from .planner import FederatedPhysicalPlanner, FederatedPlanningPolicy
from .postgres import PostgresSourceCapabilities
from .registry import SourceCapabilityRegistry

__all__ = [
    "CandidatePlan",
    "FederatedContributor",
    "FederatedPhysicalPlanner",
    "FederatedPlanningPolicy",
    "InMemoryAssemblyPlan",
    "KeySetPlan",
    "KeyTransferPlan",
    "PhysicalQueryPlan",
    "PostgresSourceCapabilities",
    "SingleSourcePlan",
    "SourceCapabilities",
    "SourceCapabilityProvider",
    "SourceCapabilityRegistry",
    "SourceScanPlan",
]
