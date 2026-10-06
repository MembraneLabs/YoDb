"""Three-file YAML catalog loader for YoDb V0.1.

The public YAML shape is deliberately backend-neutral:
datasets -> sources/resources/physical fields -> relationships.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
import re
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator


API_VERSION = "yodb/v0.1"
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]*$")


class CatalogValidationError(ValueError):
    """Raised when catalog YAML cannot form a valid static catalog."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class LogicalType(str, Enum):
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


class Visibility(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"


class SourceKind(str, Enum):
    POSTGRES = "postgres"
    NEO4J = "neo4j"


class Cardinality(str, Enum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class FieldSpec(StrictModel):
    type: LogicalType
    description: str
    aliases: tuple[str, ...] = ()
    example_values: tuple[Any, ...] = ()
    visibility: Visibility = Visibility.PUBLIC
    semantic_eligible: bool = False


class DatasetSpec(StrictModel):
    description: str
    aliases: tuple[str, ...] = ()
    fields: dict[str, FieldSpec]

    @model_validator(mode="after")
    def requires_public_id(self) -> "DatasetSpec":
        identifier = self.fields.get("id")
        if identifier is None or identifier.type is not LogicalType.ID:
            raise ValueError("must declare an 'id' field with type 'id'")
        if identifier.visibility is not Visibility.PUBLIC:
            raise ValueError("the 'id' field must have public visibility")
        return self


class CatalogMetadata(StrictModel):
    name: str
    version: int = Field(gt=0)


class DatasetsDocument(StrictModel):
    api_version: Literal[API_VERSION]
    catalog: CatalogMetadata
    datasets: dict[str, DatasetSpec]


class SourceFieldSpec(StrictModel):
    physical_name: str


class VectorMetric(str, Enum):
    COSINE = "cosine"
    L2 = "l2"
    INNER_PRODUCT = "inner_product"


class EmbeddingBinding(StrictModel):
    """A vector column holding precomputed embeddings of one logical text field.

    The column sits either in the source that owns the text, or in a separate
    *vector store* source that represents the same dataset (identity only).

    The embedding is a physical retrieval aid for ``SemanticFilter``; callers
    never name it.  ``model``, ``dimensions``, ``metric`` and ``version`` must
    describe how the stored vectors were produced so a query embedding is only
    ever compared with vectors of the same space.
    """

    column: str
    model: str
    dimensions: int = Field(gt=0)
    metric: VectorMetric = VectorMetric.COSINE
    version: str = "1"


class SourceDatasetSpec(StrictModel):
    resource: str
    identity: tuple[str, ...] = Field(min_length=1)
    fields: dict[str, SourceFieldSpec]
    # Logical text field name -> the vector column embedding it in this source.
    # A binding may sit in a *vector store* representation that maps only the identity: the
    # logical text field is then owned by another source, and this source holds only its vectors.
    embeddings: dict[str, EmbeddingBinding] = Field(default_factory=dict)


class SourceSpec(StrictModel):
    kind: SourceKind
    connection_ref: str
    read_only: Literal[True]
    datasets: dict[str, SourceDatasetSpec]


class DatasetResolution(StrictModel):
    identity_source: str
    field_sources: dict[str, str]


class SourcesDocument(StrictModel):
    api_version: Literal[API_VERSION]
    sources: dict[str, SourceSpec]
    resolution: dict[str, DatasetResolution]


class RelationEndpoint(StrictModel):
    source: str
    field: str


class RelationshipImplementation(StrictModel):
    from_endpoint: RelationEndpoint = Field(alias="from")
    to_endpoint: RelationEndpoint = Field(alias="to")
    edge_type: str | None = None


class RelationshipSpec(StrictModel):
    from_dataset: str = Field(alias="from")
    to_dataset: str = Field(alias="to")
    description: str
    aliases: tuple[str, ...] = ()
    cardinality: Cardinality
    direction: Literal["uni", "bi"]
    implementations: tuple[RelationshipImplementation, ...] = Field(min_length=1)


class RelationsDocument(StrictModel):
    api_version: Literal[API_VERSION]
    relationships: dict[str, RelationshipSpec]


class Catalog(StrictModel):
    metadata: CatalogMetadata
    datasets: dict[str, DatasetSpec]
    sources: dict[str, SourceSpec]
    resolution: dict[str, DatasetResolution]
    relationships: dict[str, RelationshipSpec]


def load_catalog(directory: str | Path) -> Catalog:
    """Load and statically validate ``datasets.yaml``, ``sources.yaml``, and ``relations.yaml``.

    The function performs no network I/O and never resolves connection
    references. It is safe to run during local development and CI.
    """

    root = Path(directory)
    datasets_document = _load_document(root / "datasets.yaml", DatasetsDocument)
    sources = _load_document(root / "sources.yaml", SourcesDocument)
    relations = _load_document(root / "relations.yaml", RelationsDocument)

    catalog = Catalog(
        metadata=datasets_document.catalog,
        datasets=datasets_document.datasets,
        sources=sources.sources,
        resolution=sources.resolution,
        relationships=relations.relationships,
    )
    _validate_catalog(catalog)
    return catalog


def _load_document(path: Path, model: type[StrictModel]) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except FileNotFoundError as error:
        raise CatalogValidationError(f"Required catalog file is missing: {path.name}") from error
    except yaml.YAMLError as error:
        raise CatalogValidationError(f"Invalid YAML in {path.name}: {error}") from error

    if not isinstance(raw, dict):
        raise CatalogValidationError(f"{path.name} must contain a YAML mapping at its root")

    try:
        return model.model_validate(raw)
    except ValidationError as error:
        raise CatalogValidationError(f"Invalid {path.name}: {error}") from error


def _validate_catalog(catalog: Catalog) -> None:
    _validate_identifier("catalog.name", catalog.metadata.name)
    _validate_named_mapping("datasets", catalog.datasets)
    _validate_named_mapping("sources", catalog.sources)
    _validate_named_mapping("relationships", catalog.relationships)

    for dataset_name, dataset in catalog.datasets.items():
        _validate_named_mapping(f"datasets.{dataset_name}.fields", dataset.fields)

    for source_name, source in catalog.sources.items():
        if not source.connection_ref.strip():
            raise CatalogValidationError(f"sources.{source_name}.connection_ref must not be blank")
        _validate_named_mapping(f"sources.{source_name}.datasets", source.datasets)
        for dataset_name, representation in source.datasets.items():
            if dataset_name not in catalog.datasets:
                raise CatalogValidationError(
                    f"sources.{source_name}.datasets references unknown dataset '{dataset_name}'"
                )
            if not representation.resource.strip():
                raise CatalogValidationError(
                    f"sources.{source_name}.datasets.{dataset_name}.resource must not be blank"
                )
            _validate_named_mapping(
                f"sources.{source_name}.datasets.{dataset_name}.fields",
                representation.fields,
            )
            for field_name, binding in representation.fields.items():
                if field_name not in catalog.datasets[dataset_name].fields:
                    raise CatalogValidationError(
                        f"sources.{source_name}.datasets.{dataset_name}.fields references "
                        f"unknown field '{field_name}'"
                    )
                if not binding.physical_name.strip():
                    raise CatalogValidationError(
                        f"sources.{source_name}.datasets.{dataset_name}.fields.{field_name}.physical_name "
                        "must not be blank"
                    )
            for identity_field in representation.identity:
                if identity_field not in representation.fields:
                    raise CatalogValidationError(
                        f"sources.{source_name}.datasets.{dataset_name}.identity field "
                        f"'{identity_field}' is not mapped"
                    )
            for field_name, embedding in representation.embeddings.items():
                location = f"sources.{source_name}.datasets.{dataset_name}.embeddings.{field_name}"
                logical_field = catalog.datasets[dataset_name].fields.get(field_name)
                if logical_field is None:
                    raise CatalogValidationError(f"{location} must name a logical field of the dataset")
                if field_name in representation.fields:
                    pass                      # the vectors sit next to the text
                elif "id" not in representation.identity:
                    raise CatalogValidationError(
                        f"{location}: a vector store representation must map the dataset identity 'id'"
                    )
                if not logical_field.semantic_eligible or logical_field.type not in {
                    LogicalType.STRING,
                    LogicalType.TEXT,
                }:
                    raise CatalogValidationError(
                        f"{location} requires a string/text field with semantic_eligible: true"
                    )
                if not embedding.column.strip() or not embedding.model.strip() or not embedding.version.strip():
                    raise CatalogValidationError(f"{location} column, model and version must not be blank")
    _validate_embedding_owners(catalog)

    _validate_resolutions(catalog)
    _validate_relationships(catalog)


def _validate_embedding_owners(catalog: Catalog) -> None:
    """One logical text field has at most one embedding declaration across all sources."""

    seen: dict[tuple[str, str], str] = {}
    for source_name, source in catalog.sources.items():
        for dataset_name, representation in source.datasets.items():
            for field_name in representation.embeddings:
                key = (dataset_name, field_name)
                if key in seen:
                    raise CatalogValidationError(
                        f"embeddings for '{dataset_name}.{field_name}' are declared by both "
                        f"'{seen[key]}' and '{source_name}'; declare them once"
                    )
                seen[key] = source_name


def _validate_resolutions(catalog: Catalog) -> None:
    if set(catalog.resolution) != set(catalog.datasets):
        missing = sorted(set(catalog.datasets) - set(catalog.resolution))
        extra = sorted(set(catalog.resolution) - set(catalog.datasets))
        details = []
        if missing:
            details.append(f"missing resolutions for: {', '.join(missing)}")
        if extra:
            details.append(f"unknown dataset resolutions: {', '.join(extra)}")
        raise CatalogValidationError("resolution must cover every dataset (" + "; ".join(details) + ")")

    for dataset_name, resolution in catalog.resolution.items():
        _validate_source_field(catalog, dataset_name, resolution.identity_source, "id", "identity_source")
        identity_representation = catalog.sources[resolution.identity_source].datasets[dataset_name]
        if "id" not in identity_representation.identity:
            raise CatalogValidationError(
                f"resolution.{dataset_name}.identity_source '{resolution.identity_source}' must use 'id' "
                "as an identity field"
            )

        expected_fields = set(catalog.datasets[dataset_name].fields)
        actual_fields = set(resolution.field_sources)
        if expected_fields != actual_fields:
            missing = sorted(expected_fields - actual_fields)
            extra = sorted(actual_fields - expected_fields)
            details = []
            if missing:
                details.append(f"missing field sources: {', '.join(missing)}")
            if extra:
                details.append(f"unknown fields: {', '.join(extra)}")
            raise CatalogValidationError(
                f"resolution.{dataset_name}.field_sources must cover every field "
                f"({'; '.join(details)})"
            )

        if resolution.field_sources["id"] != resolution.identity_source:
            raise CatalogValidationError(
                f"resolution.{dataset_name}.field_sources.id must match identity_source"
            )

        for field_name, source_name in resolution.field_sources.items():
            _validate_source_field(catalog, dataset_name, source_name, field_name, "field_sources")


def _validate_relationships(catalog: Catalog) -> None:
    for relationship_name, relationship in catalog.relationships.items():
        if relationship.from_dataset not in catalog.datasets:
            raise CatalogValidationError(
                f"relationships.{relationship_name}.from references unknown dataset "
                f"'{relationship.from_dataset}'"
            )
        if relationship.to_dataset not in catalog.datasets:
            raise CatalogValidationError(
                f"relationships.{relationship_name}.to references unknown dataset "
                f"'{relationship.to_dataset}'"
            )

        for implementation in relationship.implementations:
            _validate_source_field(
                catalog,
                relationship.from_dataset,
                implementation.from_endpoint.source,
                implementation.from_endpoint.field,
                f"relationships.{relationship_name}.implementations.from",
            )
            _validate_source_field(
                catalog,
                relationship.to_dataset,
                implementation.to_endpoint.source,
                implementation.to_endpoint.field,
                f"relationships.{relationship_name}.implementations.to",
            )

            if implementation.edge_type is not None:
                _validate_graph_implementation(catalog, relationship_name, relationship, implementation)
            else:
                from_type = catalog.datasets[relationship.from_dataset].fields[implementation.from_endpoint.field].type
                to_type = catalog.datasets[relationship.to_dataset].fields[implementation.to_endpoint.field].type
                if from_type is not to_type:
                    raise CatalogValidationError(
                        f"relationships.{relationship_name}.implementations compares "
                        f"'{relationship.from_dataset}.{implementation.from_endpoint.field}' ({from_type.value}) with "
                        f"'{relationship.to_dataset}.{implementation.to_endpoint.field}' ({to_type.value}); "
                        "the two fields must have the same type"
                    )


def _validate_source_field(
    catalog: Catalog,
    dataset_name: str,
    source_name: str,
    field_name: str,
    location: str,
) -> None:
    source = catalog.sources.get(source_name)
    if source is None:
        raise CatalogValidationError(f"{location} references unknown source '{source_name}'")
    representation = source.datasets.get(dataset_name)
    if representation is None:
        raise CatalogValidationError(
            f"{location} source '{source_name}' does not represent dataset '{dataset_name}'"
        )
    if field_name not in representation.fields:
        raise CatalogValidationError(
            f"{location} field '{field_name}' is not mapped for dataset '{dataset_name}' "
            f"in source '{source_name}'"
        )


def _validate_graph_implementation(
    catalog: Catalog,
    relationship_name: str,
    relationship: RelationshipSpec,
    implementation: RelationshipImplementation,
) -> None:
    if not implementation.edge_type.strip():
        raise CatalogValidationError(
            f"relationships.{relationship_name}.implementations.edge_type must not be blank"
        )

    from_source = implementation.from_endpoint.source
    to_source = implementation.to_endpoint.source
    if from_source != to_source:
        raise CatalogValidationError(
            f"relationships.{relationship_name}.implementations with edge_type must use one graph source"
        )

    source = catalog.sources[from_source]
    if source.kind is not SourceKind.NEO4J:
        raise CatalogValidationError(
            f"relationships.{relationship_name}.implementations.edge_type source '{from_source}' must be neo4j"
        )

    for dataset_name in (relationship.from_dataset, relationship.to_dataset):
        if dataset_name not in source.datasets:
            raise CatalogValidationError(
                f"relationships.{relationship_name}.implementations edge source '{from_source}' "
                f"does not represent dataset '{dataset_name}'"
            )


def _validate_named_mapping(location: str, values: dict[str, Any]) -> None:
    if not values:
        raise CatalogValidationError(f"{location} must not be empty")
    for name in values:
        _validate_identifier(location, name)


def _validate_identifier(location: str, value: str) -> None:
    if not _IDENTIFIER.fullmatch(value):
        raise CatalogValidationError(
            f"{location} identifier '{value}' must use lower_snake_case"
        )
