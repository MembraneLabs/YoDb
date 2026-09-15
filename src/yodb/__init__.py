"""YoDb V0.1 catalog loading, inspection contracts, and static validation."""

from .catalog import Catalog, CatalogValidationError, load_catalog
from .errors import ErrorCode, ErrorDetail, SourceInspectionError, YoDbError
from .inspection import (
    FindingSeverity,
    InspectionCapability,
    InspectionRequest,
    GraphRelationshipType,
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
from .postgres import PostgresCatalogValidator, PostgresSourceInspector

__all__ = [
    "Catalog",
    "CatalogValidationError",
    "ErrorCode",
    "ErrorDetail",
    "FindingSeverity",
    "GraphRelationshipType",
    "InspectionCapability",
    "InspectionRequest",
    "PhysicalField",
    "PhysicalCheckConstraint",
    "PhysicalForeignKey",
    "PhysicalIndex",
    "PhysicalKey",
    "PhysicalResource",
    "PhysicalUniqueConstraint",
    "PostgresCatalogValidator",
    "PostgresSourceInspector",
    "ResourceKind",
    "SourceCatalogValidator",
    "SourceInspectionError",
    "SourceInspection",
    "SourceInspector",
    "SourceValidationReport",
    "ValidationFinding",
    "YoDbError",
    "load_catalog",
]
