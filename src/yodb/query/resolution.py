"""Resolve one bound logical query into safe source-local participation objects.

This module does not choose a join order, generate SQL, or open a connection.
It only answers which declared sources own the query's logical fields and how
those sources can be linked by the dataset's stable logical identity.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from enum import Enum

from ..catalog import SourceKind
from ..errors import ErrorCode, ErrorDetail, QueryError
from ..runtime.contracts import CatalogEvaluation
from .fingerprint import catalog_fingerprint
from .models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundField,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    BoundQuery,
)


class QuerySourceShape(str, Enum):
    """Whether the current root query can stay within one physical source."""

    SINGLE_SOURCE = "single_source"
    MULTI_SOURCE = "multi_source"


class FieldUse(str, Enum):
    """The logical role a field plays in a source-local query fragment."""

    IDENTITY = "identity"
    PROJECTION = "projection"
    FILTER = "filter"
    ORDER = "order"


@dataclass(frozen=True)
class ResolvedField:
    """One vetted logical field and its approved physical source mapping."""

    field: BoundField
    source_name: str
    source_kind: SourceKind
    connection_ref: str
    resource: str
    physical_name: str
    uses: frozenset[FieldUse]


@dataclass(frozen=True)
class SingleSourceQueryBinding:
    """The complete source-local participation for one logical root dataset.

    The global Boolean expression remains on :class:`SourceResolvedQuery`.
    A future planner decides which source-local predicates can be pushed down
    and how source-local candidate ID sets are combined without changing query
    meaning.
    """

    source_name: str
    source_kind: SourceKind
    connection_ref: str
    resource: str
    logical_id: ResolvedField
    fields: tuple[ResolvedField, ...]

    @property
    def projection_fields(self) -> tuple[ResolvedField, ...]:
        return _fields_for_use(self.fields, FieldUse.PROJECTION)

    @property
    def filter_fields(self) -> tuple[ResolvedField, ...]:
        return _fields_for_use(self.fields, FieldUse.FILTER)

    @property
    def order_fields(self) -> tuple[ResolvedField, ...]:
        return _fields_for_use(self.fields, FieldUse.ORDER)


@dataclass(frozen=True)
class LogicalIdLink:
    """An approved same-dataset link between two source-local ID bindings."""

    from_source: str
    from_logical_id: ResolvedField
    to_source: str
    to_logical_id: ResolvedField


@dataclass(frozen=True)
class SourceResolvedQuery:
    """A bound query classified by its declared source participation.

    ``single_source`` is immediately eligible for the first PostgreSQL
    compiler. ``multi_source`` deliberately retains its source-local objects
    and logical-ID links for the later record-assembly planner; it is not yet
    an executable distributed query plan.
    """

    query: BoundQuery
    shape: QuerySourceShape
    identity_source: SingleSourceQueryBinding
    sources: tuple[SingleSourceQueryBinding, ...]
    logical_id_links: tuple[LogicalIdLink, ...]


def resolve_query_sources(query: BoundQuery, active: CatalogEvaluation) -> SourceResolvedQuery:
    """Resolve fields against the exact active catalog that bound the query.

    Every participating source must declare a unique mapping for the root
    dataset's logical ``id``. This is the only automatic same-dataset link;
    source matching is never inferred from values, names, or foreign keys.
    """

    catalog = active.catalog
    actual_catalog_fingerprint = catalog_fingerprint(catalog)
    if query.catalog_fingerprint != actual_catalog_fingerprint:
        _fail(
            ErrorCode.QUERY_CATALOG_MISMATCH,
            "The query was bound against a different active catalog snapshot.",
        )

    resolution = catalog.resolution.get(query.root.name)
    if resolution is None:
        _fail(
            ErrorCode.SOURCE_BINDING_UNAVAILABLE,
            f"Dataset '{query.root.name}' has no source resolution.",
            "from.dataset",
        )
    if resolution.field_sources.get("id") != resolution.identity_source:
        _fail(
            ErrorCode.SOURCE_BINDING_UNAVAILABLE,
            "The dataset identity source must also be the declared source for logical field 'id'.",
            f"resolution.{query.root.name}.field_sources.id",
        )

    field_uses = _collect_field_uses(query)
    source_fields: dict[str, dict[str, frozenset[FieldUse]]] = defaultdict(dict)
    for field_name, uses in field_uses.items():
        source_name = resolution.field_sources.get(field_name)
        if source_name is None:
            _fail(
                ErrorCode.SOURCE_BINDING_UNAVAILABLE,
                f"No source owns logical field '{field_name}' for dataset '{query.root.name}'.",
                f"resolution.{query.root.name}.field_sources.{field_name}",
            )
        source_fields[source_name][field_name] = frozenset(uses)

    # The identity source is the anchor even if all user-visible fields came
    # from a secondary source. It is already present because logical id is a
    # required bound-query field.
    bindings = {
        source_name: _resolve_source_binding(
            source_name,
            query,
            active,
            fields,
        )
        for source_name, fields in source_fields.items()
    }
    identity = bindings.get(resolution.identity_source)
    if identity is None:
        raise AssertionError("logical id must require the configured identity source")

    ordered_sources = (identity, *tuple(bindings[name] for name in sorted(bindings) if name != identity.source_name))
    links = tuple(
        LogicalIdLink(
            from_source=identity.source_name,
            from_logical_id=identity.logical_id,
            to_source=source.source_name,
            to_logical_id=source.logical_id,
        )
        for source in ordered_sources[1:]
    )
    shape = QuerySourceShape.SINGLE_SOURCE if len(ordered_sources) == 1 else QuerySourceShape.MULTI_SOURCE
    return SourceResolvedQuery(
        query=query,
        shape=shape,
        identity_source=identity,
        sources=ordered_sources,
        logical_id_links=links,
    )


def _resolve_source_binding(
    source_name: str,
    query: BoundQuery,
    active: CatalogEvaluation,
    fields: dict[str, frozenset[FieldUse]],
) -> SingleSourceQueryBinding:
    catalog = active.catalog
    source = catalog.sources.get(source_name)
    if source is None:
        _fail(ErrorCode.SOURCE_BINDING_UNAVAILABLE, f"Unknown source '{source_name}'.")
    representation = source.datasets.get(query.root.name)
    if representation is None:
        _fail(
            ErrorCode.SOURCE_BINDING_UNAVAILABLE,
            f"Source '{source_name}' does not represent dataset '{query.root.name}'.",
        )

    logical_id = _resolve_logical_id(source_name, query, active)
    resolved_fields = tuple(
        _resolve_field(source_name, query, active, field_name, uses)
        for field_name, uses in sorted(fields.items())
    )
    return SingleSourceQueryBinding(
        source_name=source_name,
        source_kind=source.kind,
        connection_ref=source.connection_ref,
        resource=representation.resource,
        logical_id=logical_id,
        fields=resolved_fields,
    )


def _resolve_logical_id(
    source_name: str,
    query: BoundQuery,
    active: CatalogEvaluation,
) -> ResolvedField:
    catalog = active.catalog
    source = catalog.sources[source_name]
    representation = source.datasets[query.root.name]
    if "id" not in representation.fields or "id" not in representation.identity:
        _fail(
            ErrorCode.SOURCE_LOGICAL_ID_UNAVAILABLE,
            (
                f"Source '{source_name}' cannot contribute to dataset '{query.root.name}' because it does "
                "not declare logical field 'id' as a source identity."
            ),
            f"sources.{source_name}.datasets.{query.root.name}.identity",
        )
    return _resolve_field(source_name, query, active, "id", frozenset({FieldUse.IDENTITY}))


def _resolve_field(
    source_name: str,
    query: BoundQuery,
    active: CatalogEvaluation,
    field_name: str,
    uses: frozenset[FieldUse],
) -> ResolvedField:
    catalog = active.catalog
    source = catalog.sources[source_name]
    representation = source.datasets[query.root.name]
    physical_binding = representation.fields.get(field_name)
    if physical_binding is None:
        _fail(
            ErrorCode.SOURCE_BINDING_UNAVAILABLE,
            f"Source '{source_name}' has no approved mapping for field '{field_name}'.",
            f"sources.{source_name}.datasets.{query.root.name}.fields.{field_name}",
        )
    bound_field = _bound_field_for_name(query, field_name)
    return ResolvedField(
        field=bound_field,
        source_name=source_name,
        source_kind=source.kind,
        connection_ref=source.connection_ref,
        resource=representation.resource,
        physical_name=physical_binding.physical_name,
        uses=uses,
    )


def _collect_field_uses(query: BoundQuery) -> dict[str, set[FieldUse]]:
    uses: dict[str, set[FieldUse]] = defaultdict(set)
    uses["id"].add(FieldUse.IDENTITY)
    for field in query.select:
        uses[field.name].add(FieldUse.PROJECTION)
    for field in _filter_fields(query.where):
        uses[field.name].add(FieldUse.FILTER)
    for term in query.order_by:
        uses[term.field.name].add(FieldUse.ORDER)
    return uses


def _filter_fields(expression: BoundFilterExpression | None) -> tuple[BoundField, ...]:
    if expression is None:
        return ()
    if isinstance(expression, BoundPredicate):
        return (expression.field,)
    if isinstance(expression, (BoundAllExpression, BoundAnyExpression)):
        return tuple(field for child in expression.expressions for field in _filter_fields(child))
    if isinstance(expression, BoundNotExpression):
        return _filter_fields(expression.expression)
    raise AssertionError(f"Unknown bound expression: {expression!r}")


def _bound_field_for_name(query: BoundQuery, field_name: str) -> BoundField:
    spec = query.root.spec.fields.get(field_name)
    if spec is None:
        raise AssertionError(f"Bound field '{field_name}' was not found in the root dataset")
    return BoundField(
        dataset_name=query.root.name,
        scope=query.root.scope,
        name=field_name,
        spec=spec,
    )


def _fields_for_use(
    fields: tuple[ResolvedField, ...],
    use: FieldUse,
) -> tuple[ResolvedField, ...]:
    return tuple(field for field in fields if use in field.uses)


def _fail(code: ErrorCode, message: str, location: str | None = None) -> None:
    raise QueryError(ErrorDetail(code=code, message=message, retryable=False, location=location))
