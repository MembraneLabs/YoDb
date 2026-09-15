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
from .errors import ErrorCode, ErrorDetail, SourceInspectionError, YoDbError
from .postgres import PostgresCatalogValidator, PostgresSourceInspector

__all__ = [
    "ErrorCode",
    "ErrorDetail",
    "FindingSeverity",
    "GraphRelationshipType",
    "InspectionCapability",
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
    "SourceInspectionError",
    "SourceInspector",
    "SourceValidationReport",
    "ValidationFinding",
    "YoDbError",
]
