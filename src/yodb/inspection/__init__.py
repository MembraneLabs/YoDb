"""Source inspection contracts, structured errors, and provider adapters."""

from .contracts import (
    FindingSeverity,
    GraphRelationshipType,
    InspectionCapability,
    InspectionRequest,
    PhysicalCheckConstraint,
    PhysicalField,
    PhysicalForeignKey,
    PhysicalIndex,
    PhysicalKey,
    PhysicalResource,
    PhysicalUniqueConstraint,
    ResourceKind,
    SourceCatalogValidator,
    SourceInspection,
    SourceInspector,
    SourceValidationReport,
    ValidationFinding,
)
from ..errors import ErrorCode, ErrorDetail, SourceInspectionError, YoDbError
from .postgres import PostgresCatalogValidator, PostgresSourceInspector
from .registry import InspectionAdapterBinding, SourceInspectionRegistry

__all__ = [
    "ErrorCode",
    "ErrorDetail",
    "FindingSeverity",
    "GraphRelationshipType",
    "InspectionCapability",
    "InspectionAdapterBinding",
    "InspectionRequest",
    "PhysicalCheckConstraint",
    "PhysicalField",
    "PhysicalForeignKey",
    "PhysicalIndex",
    "PhysicalKey",
    "PhysicalResource",
    "PhysicalUniqueConstraint",
    "PostgresCatalogValidator",
    "PostgresSourceInspector",
    "ResourceKind",
    "SourceCatalogValidator",
    "SourceInspection",
    "SourceInspectionRegistry",
    "SourceInspectionError",
    "SourceInspector",
    "SourceValidationReport",
    "ValidationFinding",
    "YoDbError",
]
