"""YoDb V0.1 catalog loading, inspection contracts, and static validation."""

from .catalog import Catalog, CatalogValidationError, load_catalog
from .errors import ErrorCode, ErrorDetail, SourceInspectionError, YoDbError
from .inspection import (
    FindingSeverity,
    InspectionCapability,
    InspectionRequest,
    PhysicalField,
    PhysicalForeignKey,
    PhysicalIndex,
    PhysicalResource,
    ResourceKind,
    SourceCatalogValidator,
    SourceInspection,
    SourceInspector,
    SourceValidationReport,
    ValidationFinding,
)

__all__ = [
    "Catalog",
    "CatalogValidationError",
    "ErrorCode",
    "ErrorDetail",
    "FindingSeverity",
    "InspectionCapability",
    "InspectionRequest",
    "PhysicalField",
    "PhysicalForeignKey",
    "PhysicalIndex",
    "PhysicalResource",
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
