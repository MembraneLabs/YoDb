"""Backend-neutral contracts for source inspection and catalog validation.

Inspectors discover *physical* facts about one configured source.  They do not
infer business meaning or mutate a catalog.  Validators consume those facts
and an already-loaded :class:`~yodb.catalog.Catalog` to report whether the
user-authored bindings can be activated safely.

Concrete adapters (for example PostgreSQL or Neo4j) own their connection and
credential resolution internally.  This module deliberately has no database
driver dependency.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .catalog import Catalog, SourceKind, SourceSpec


class InspectionModel(BaseModel):
    """Immutable, strict value model shared by inspection adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ResourceKind(str, Enum):
    """A physical resource shape that an adapter can inspect."""

    TABLE = "table"
    VIEW = "view"
    NODE_LABEL = "node_label"
    RELATIONSHIP_TYPE = "relationship_type"


class InspectionCapability(str, Enum):
    """Physical facts an adapter was able to discover for a source."""

    RELATIONAL_SCHEMA = "relational_schema"
    GRAPH_SCHEMA = "graph_schema"
    PRIMARY_KEYS = "primary_keys"
    FOREIGN_KEYS = "foreign_keys"
    UNIQUE_CONSTRAINTS = "unique_constraints"
    INDEXES = "indexes"
    VECTOR_COLUMNS = "vector_columns"
    VECTOR_INDEXES = "vector_indexes"


class FindingSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class InspectionRequest(InspectionModel):
    """The configured source an adapter should inspect.

    ``source`` is the source definition from ``sources.yaml``.  An adapter
    receives any connection resolver through its constructor instead of this
    request, keeping secret resolution out of the inspection contract.
    """

    source_name: str = Field(min_length=1)
    source: SourceSpec


class PhysicalField(InspectionModel):
    """A field/property reported by a physical resource.

    ``native_type`` is adapter-native (for example ``uuid`` or ``VARCHAR`` in
    PostgreSQL).  ``nullable`` is optional because some providers, including
    graph stores, cannot always establish it from schema metadata alone.
    """

    name: str = Field(min_length=1)
    native_type: str = Field(min_length=1)
    type_family: str = Field(min_length=1)
    nullable: bool | None = None
    default: str | None = None
    generated: bool | None = None
    dimensions: int | None = Field(default=None, gt=0)


class PhysicalKey(InspectionModel):
    """A named primary or unique key on a physical resource."""

    name: str = Field(min_length=1)
    fields: tuple[str, ...] = Field(min_length=1)


class PhysicalUniqueConstraint(PhysicalKey):
    """A unique constraint, including PostgreSQL NULL-distinct behavior."""

    nulls_distinct: bool | None = None


class PhysicalForeignKey(InspectionModel):
    """A factual foreign-key constraint reported by a source."""

    name: str | None = None
    fields: tuple[str, ...] = Field(min_length=1)
    target_resource: str = Field(min_length=1)
    target_fields: tuple[str, ...] = Field(min_length=1)
    on_update: str | None = None
    on_delete: str | None = None
    deferrable: bool | None = None
    initially_deferred: bool | None = None


class PhysicalCheckConstraint(InspectionModel):
    """A provider-reported check expression; it is diagnostic only in V0.1."""

    name: str = Field(min_length=1)
    expression: str = Field(min_length=1)


class PhysicalIndex(InspectionModel):
    """An index or provider-equivalent access path on one resource."""

    name: str = Field(min_length=1)
    fields: tuple[str, ...] = ()
    method: str | None = None
    unique: bool = False
    include: tuple[str, ...] = ()
    predicate: str | None = None
    definition: str | None = None
    valid: bool | None = None
    state: str | None = None
    provider: str | None = None
    owning_constraint: str | None = None


class PhysicalResource(InspectionModel):
    """A table, view, graph label, or other adapter-supported resource."""

    name: str = Field(min_length=1)
    kind: ResourceKind
    fields: dict[str, PhysicalField]
    primary_key: PhysicalKey | None = None
    unique_constraints: tuple[PhysicalUniqueConstraint, ...] = ()
    foreign_keys: tuple[PhysicalForeignKey, ...] = ()
    check_constraints: tuple[PhysicalCheckConstraint, ...] = ()
    indexes: tuple[PhysicalIndex, ...] = ()


class GraphRelationshipType(PhysicalResource):
    """Facts about a Neo4j relationship type and its observed endpoints."""

    from_labels: tuple[str, ...] = ()
    to_labels: tuple[str, ...] = ()


class SourceInspection(InspectionModel):
    """An immutable snapshot of physical facts for exactly one source."""

    source_name: str = Field(min_length=1)
    source_kind: SourceKind
    inspected_at: datetime
    engine_version: str | None = None
    capabilities: frozenset[InspectionCapability] = frozenset()
    resources: dict[str, PhysicalResource] = {}
    relationship_types: dict[str, GraphRelationshipType] = {}
    extensions: dict[str, str] = {}
    provider_metadata: dict[str, Any] = {}


class ValidationFinding(InspectionModel):
    """One actionable result of comparing catalog claims with source facts."""

    severity: FindingSeverity
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    location: str = Field(min_length=1)


class SourceValidationReport(InspectionModel):
    """The non-mutating validation outcome for one source inspection."""

    source_name: str = Field(min_length=1)
    inspected_at: datetime
    findings: tuple[ValidationFinding, ...] = ()

    @property
    def is_valid(self) -> bool:
        """Whether no error-severity findings prevent catalog activation."""

        return not any(finding.severity is FindingSeverity.ERROR for finding in self.findings)


@runtime_checkable
class SourceInspector(Protocol):
    """Adapter contract for factual inspection of one configured source."""

    @property
    def source_kind(self) -> SourceKind:
        """The catalog source kind this adapter supports."""

    def inspect(self, request: InspectionRequest) -> SourceInspection:
        """Inspect the configured source without changing source data."""


@runtime_checkable
class SourceCatalogValidator(Protocol):
    """Adapter-neutral contract for validating one inspection against a catalog."""

    def validate(self, catalog: Catalog, inspection: SourceInspection) -> SourceValidationReport:
        """Return factual validation findings; never infer or rewrite mappings."""
