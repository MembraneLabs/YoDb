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
    SemanticVerify,
    SourceOperationRequest,
    SourcePlanningAdapter,
    VectorSearch,
)
from .adapter import CapabilityPlanningAdapter
from .capabilities import (
    BooleanOperator,
    KeyLookupCapability,
    SourceCapabilities,
    TextOrdering,
    VectorSearchCapability,
)
from .postgres import POSTGRES_CAPABILITIES, PostgresPlanningAdapter
from .registry import SourcePlanningRegistry
from .planner import FederatedPhysicalPlanner, PlannerPolicy, SemanticPlanPreference, SemanticPolicy

__all__ = [
    "BooleanOperator",
    "CapabilityPlanningAdapter",
    "KeyLookupCapability",
    "POSTGRES_CAPABILITIES",
    "SourceCapabilities",
    "TextOrdering",
    "VectorSearchCapability",
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
    "SemanticPlanPreference",
    "SemanticPolicy",
    "SemanticVerify",
    "SourceOperationRequest",
    "SourcePlanningAdapter",
    "SourcePlanningRegistry",
    "VectorSearch",
]
