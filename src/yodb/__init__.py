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

__all__ = [
    "Cardinality",
    "CatalogSpec",
    "Consistency",
    "DatasetSpec",
    "FieldSpec",
    "FieldType",
    "IndexKind",
    "IndexSpec",
    "RelationshipSpec",
    "SpecValidationError",
]
