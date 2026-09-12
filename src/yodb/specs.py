"""Logical schema specifications.

These types intentionally describe *what* an application needs, never a
physical table, database, query language, or vendor-specific index.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


_UNSET = object()


class SpecValidationError(ValueError):
    """Raised when a logical data-model specification is invalid."""


class FieldType(StrEnum):
    """Canonical scalar values supported in the first logical schema version."""

    ID = "id"
    STRING = "string"
    TEXT = "text"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    TIMESTAMP = "timestamp"
    UUID = "uuid"
    JSON = "json"
    BYTES = "bytes"


class Consistency(StrEnum):
    STRONG = "strong"
    EVENTUAL = "eventual"


class Cardinality(StrEnum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class IndexKind(StrEnum):
    VECTOR = "vector"
    LEXICAL = "lexical"
    FILTER = "filter"
    SORT = "sort"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """The schema and query capabilities of one record or edge field."""

    type: FieldType
    required: bool = False
    nullable: bool = False
    repeated: bool = False
    filterable: bool = False
    sortable: bool = False
    searchable: bool = False
    default: Any = _UNSET
    description: str | None = None

    def __post_init__(self) -> None:
        if self.type is FieldType.ID and (self.nullable or self.repeated):
            raise SpecValidationError("An id field must be singular and non-nullable.")
        if self.required and self.default is not _UNSET:
            raise SpecValidationError("A field cannot be both required and have a default.")
        if self.default is None and not self.nullable:
            raise SpecValidationError("A null default requires nullable=True.")
        if self.sortable and self.repeated:
            raise SpecValidationError("A repeated field cannot be sortable.")
        if self.searchable and self.type not in {FieldType.STRING, FieldType.TEXT}:
            raise SpecValidationError("Only string and text fields can be searchable.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "required": self.required,
            "nullable": self.nullable,
            "repeated": self.repeated,
            "filterable": self.filterable,
            "sortable": self.sortable,
            "searchable": self.searchable,
            **({"default": self.default} if self.has_default else {}),
            **({"description": self.description} if self.description is not None else {}),
        }

    @property
    def has_default(self) -> bool:
        """Whether this field supplies a literal default for missing input."""
        return self.default is not _UNSET


@dataclass(frozen=True, slots=True)
class IndexSpec:
    """A derived index over a canonical dataset field.

    A vector index derives embeddings from its source field. Raw vectors are
    deliberately not part of the canonical record schema in this first model.
    """

    name: str
    kind: IndexKind
    source_field: str
    consistency: Consistency = Consistency.EVENTUAL
    embedding_model: str | None = None
    dimensions: int | None = None
    distance: str | None = None

    def __post_init__(self) -> None:
        _require_name(self.name, "index name")
        _require_name(self.source_field, "index source field")
        if self.kind is IndexKind.VECTOR:
            if not self.embedding_model:
                raise SpecValidationError("A vector index requires an embedding_model.")
            if not self.dimensions or self.dimensions <= 0:
                raise SpecValidationError("A vector index requires positive dimensions.")
            if self.distance not in {"cosine", "dot_product", "euclidean"}:
                raise SpecValidationError(
                    "A vector index distance must be cosine, dot_product, or euclidean."
                )
        elif any(value is not None for value in (self.embedding_model, self.dimensions, self.distance)):
            raise SpecValidationError("Embedding settings are valid only for vector indexes.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "source_field": self.source_field,
            "consistency": self.consistency.value,
            "embedding_model": self.embedding_model,
            "dimensions": self.dimensions,
            "distance": self.distance,
        }


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """The logical schema for records in one named dataset."""

    name: str
    fields: Mapping[str, FieldSpec]
    indexes: tuple[IndexSpec, ...] = ()
    namespace: str = "default"
    version: int = 1
    canonical_consistency: Consistency = Consistency.STRONG
    allow_unknown_fields: bool = False

    def __post_init__(self) -> None:
        _require_name(self.name, "dataset name")
        _require_name(self.namespace, "namespace")
        if self.version < 1:
            raise SpecValidationError("A dataset version must be at least 1.")
        if not self.fields:
            raise SpecValidationError("A dataset must declare at least one field.")
        if "id" not in self.fields:
            raise SpecValidationError("A dataset must declare an 'id' field.")
        if self.fields["id"].type is not FieldType.ID:
            raise SpecValidationError("The dataset 'id' field must have type 'id'.")
        for name in self.fields:
            _require_name(name, "field name")
        seen_indexes: set[str] = set()
        for index in self.indexes:
            if index.name in seen_indexes:
                raise SpecValidationError(f"Duplicate index name: {index.name!r}.")
            seen_indexes.add(index.name)
            source = self.fields.get(index.source_field)
            if source is None:
                raise SpecValidationError(
                    f"Index {index.name!r} refers to unknown field {index.source_field!r}."
                )
            if index.kind is IndexKind.VECTOR and source.type not in {FieldType.STRING, FieldType.TEXT}:
                raise SpecValidationError("A vector index source must be a string or text field.")

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}.{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "version": self.version,
            "canonical_consistency": self.canonical_consistency.value,
            "allow_unknown_fields": self.allow_unknown_fields,
            "fields": {name: spec.to_dict() for name, spec in self.fields.items()},
            "indexes": [index.to_dict() for index in self.indexes],
        }


@dataclass(frozen=True, slots=True)
class RelationshipSpec:
    """A typed, directional edge between two datasets."""

    name: str
    from_dataset: str
    to_dataset: str
    cardinality: Cardinality
    fields: Mapping[str, FieldSpec] = field(default_factory=dict)
    namespace: str = "default"
    version: int = 1

    def __post_init__(self) -> None:
        _require_name(self.name, "relationship name")
        _require_name(self.from_dataset, "source dataset")
        _require_name(self.to_dataset, "target dataset")
        _require_name(self.namespace, "namespace")
        if self.version < 1:
            raise SpecValidationError("A relationship version must be at least 1.")
        if "id" in self.fields:
            raise SpecValidationError("Relationship fields cannot define 'id'; edges own their ID.")
        for name, spec in self.fields.items():
            _require_name(name, "relationship field name")
            if spec.type is FieldType.ID:
                raise SpecValidationError("Relationship fields cannot use type 'id'.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "version": self.version,
            "from_dataset": self.from_dataset,
            "to_dataset": self.to_dataset,
            "cardinality": self.cardinality.value,
            "fields": {name: spec.to_dict() for name, spec in self.fields.items()},
        }


@dataclass(frozen=True, slots=True)
class CatalogSpec:
    """A coherent collection of datasets and relationships.

    The catalog provides cross-spec validation before anything is persisted.
    """

    datasets: tuple[DatasetSpec, ...]
    relationships: tuple[RelationshipSpec, ...] = ()

    def __post_init__(self) -> None:
        names = {dataset.qualified_name for dataset in self.datasets}
        if len(names) != len(self.datasets):
            raise SpecValidationError("Dataset names must be unique within a namespace.")
        relationship_names = {f"{relationship.namespace}.{relationship.name}" for relationship in self.relationships}
        if len(relationship_names) != len(self.relationships):
            raise SpecValidationError("Relationship names must be unique within a namespace.")
        for relationship in self.relationships:
            from_name = f"{relationship.namespace}.{relationship.from_dataset}"
            to_name = f"{relationship.namespace}.{relationship.to_dataset}"
            if from_name not in names or to_name not in names:
                raise SpecValidationError(
                    f"Relationship {relationship.name!r} must reference existing datasets."
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "datasets": [dataset.to_dict() for dataset in self.datasets],
            "relationships": [relationship.to_dict() for relationship in self.relationships],
        }


def _require_name(value: str, label: str) -> None:
    if not value or not value.replace("_", "").replace("-", "").isalnum():
        raise SpecValidationError(
            f"Invalid {label} {value!r}; use letters, numbers, underscores, or hyphens."
        )
