"""YoDb's backend-neutral logical data model."""

from .specs import (
    Cardinality,
    CatalogSpec,
    Consistency,
    DatasetSpec,
    FieldSpec,
    FieldType,
    IndexKind,
    IndexSpec,
    RelationshipSpec,
    SpecValidationError,
)
from .errors import EdgeValidationError, RecordValidationError, ValidationViolation, YoDbError
from .memory import InMemoryCanonicalStore
from .records import CanonicalEdge, CanonicalRecord

__all__ = [
    "Cardinality",
    "CanonicalEdge",
    "CanonicalRecord",
    "CatalogSpec",
    "Consistency",
    "DatasetSpec",
    "EdgeValidationError",
    "FieldSpec",
    "FieldType",
    "IndexKind",
    "IndexSpec",
    "InMemoryCanonicalStore",
    "RecordValidationError",
    "RelationshipSpec",
    "SpecValidationError",
    "ValidationViolation",
    "YoDbError",
]
