"""Executable federated physical-plan contracts and source planning adapters."""

from .contracts import (
    CoordinatorFilter,
    CoordinatorSortPage,
    PhysicalPlan,
    PlanExplanation,
    PlanExplanationNode,
    PlanLocation,
    PlanLocationKind,
    PlanProperties,
    PlannedQuery,
    PushdownDecision,
    RecordAssembly,
    RemoteScan,
    ResultCompleteness,
    ResultProject,
    ResultShape,
    SourceOperationRequest,
    SourcePlanningAdapter,
)
from .postgres import PostgresPlanningAdapter
from .registry import SourcePlanningRegistry
from .planner import FederatedPhysicalPlanner, PlannerPolicy

__all__ = [
    "CoordinatorFilter",
    "CoordinatorSortPage",
    "FederatedPhysicalPlanner",
    "PhysicalPlan",
    "PlanExplanation",
    "PlanExplanationNode",
    "PlanLocation",
    "PlanLocationKind",
    "PlanProperties",
    "PlannedQuery",
    "PlannerPolicy",
    "PostgresPlanningAdapter",
    "PushdownDecision",
    "RecordAssembly",
    "RemoteScan",
    "ResultCompleteness",
    "ResultProject",
    "ResultShape",
    "SourceOperationRequest",
    "SourcePlanningAdapter",
    "SourcePlanningRegistry",
]
