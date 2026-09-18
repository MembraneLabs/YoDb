"""Build one executable baseline physical DAG from a resolved logical query."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json

from ..errors import ErrorCode, ErrorDetail, QueryError
from ..query.models import (
    BoundAllExpression,
    BoundFilterExpression,
    BoundPredicate,
    BoundQuery,
)
from ..query.resolution import QuerySourceShape, ResolvedField, SingleSourceQueryBinding, SourceResolvedQuery
from .contracts import (
    CoordinatorFilter,
    CoordinatorSortPage,
    PhysicalPlan,
    PlanExplanation,
    PlanExplanationNode,
    PlanProperties,
    PlannedQuery,
    RecordAssembly,
    RemoteScan,
    ResultCompleteness,
    ResultProject,
    ResultShape,
    SourceOperationRequest,
    coordinator_location,
    remote_location,
)
from .registry import SourcePlanningRegistry


@dataclass(frozen=True)
class PlannerPolicy:
    """Hard baseline planner limits; runtime enforces the same scan guards."""

    maximum_rows_per_source: int = 10_000

    def __post_init__(self) -> None:
        if self.maximum_rows_per_source < 1:
            raise ValueError("maximum_rows_per_source must be positive")


class FederatedPhysicalPlanner:
    """Create a correct, executable baseline before enumerating alternatives."""

    def __init__(self, adapters: SourcePlanningRegistry, *, policy: PlannerPolicy = PlannerPolicy()) -> None:
        self._adapters = adapters
        self._policy = policy

    def plan(self, resolved: SourceResolvedQuery) -> PlannedQuery:
        query = resolved.query
        if query.page.after is not None:
            _fail(
                ErrorCode.QUERY_FEATURE_NOT_SUPPORTED,
                "Cursor execution is unavailable until signed cursor verification is implemented.",
                "page.after",
            )
        scans = self._build_scans(resolved)
        if resolved.shape is QuerySourceShape.SINGLE_SOURCE:
            current: PhysicalPlan = scans[0]
        else:
            current = self._assembly(scans, resolved)

        # Retain complete filtering at the coordinator in V0.1, including
        # predicates already accepted remotely. This protects logical results
        # while source adapter behavior is being verified in production tests.
        current = CoordinatorFilter(
            input=current,
            expression=query.where,
            properties=_properties_from(
                current.properties,
                location=coordinator_location(),
            ),
        )
        current = CoordinatorSortPage(
            input=current,
            order_by=query.order_by,
            first=query.page.first,
            after=query.page.after,
            properties=_properties_from(
                current.properties,
                ordering=query.order_by,
                location=coordinator_location(),
            ),
        )
        projection = _result_projection(query, scans)
        current = ResultProject(
            input=current,
            projection=projection,
            properties=PlanProperties(
                output_fields=projection,
                logical_id=_field_named("id", projection),
                ids_are_unique=True,
                ordering=current.properties.ordering,
                location=coordinator_location(),
                completeness=ResultCompleteness.EXACT,
                result_shape=ResultShape.RECORDS,
                catalog_fingerprint=query.catalog_fingerprint,
            ),
        )
        fingerprint = _plan_fingerprint(current)
        explanation = PlanExplanation(
            plan_kind="single_source" if resolved.shape is QuerySourceShape.SINGLE_SOURCE else "in_memory_record_assembly",
            catalog_fingerprint=query.catalog_fingerprint,
            plan_fingerprint=fingerprint,
            nodes=tuple(_explain_nodes(current)),
        )
        return PlannedQuery(
            query=query,
            resolved=resolved,
            plan=current,
            catalog_fingerprint=query.catalog_fingerprint,
            query_fingerprint=query.query_fingerprint,
            plan_fingerprint=fingerprint,
            explain=explanation,
        )

    def _build_scans(self, resolved: SourceResolvedQuery) -> tuple[RemoteScan, ...]:
        source_filters = _source_local_filters(resolved)
        complete = resolved.shape is QuerySourceShape.SINGLE_SOURCE
        scans = []
        for source in resolved.sources:
            projection = _source_projection(source)
            requested = SourceOperationRequest(
                projection=projection,
                filter=resolved.query.where if complete else source_filters.get(source.source_name),
                order_by=resolved.query.order_by if complete else (),
                limit=_effective_limit(resolved.query) if complete else None,
                complete_result=complete,
            )
            decision = self._adapters.adapter_for(source.source_kind).plan_remote_scan(source, requested)
            if decision.accepted_projection != projection:
                _fail(
                    ErrorCode.SOURCE_CAPABILITY_UNAVAILABLE,
                    "The source planning adapter cannot provide the mandatory logical ID and projection.",
                    source.source_name,
                )
            needs_guard = not complete or decision.accepted_limit != requested.limit
            scans.append(
                RemoteScan(
                    source=source,
                    projection=projection,
                    pushed_filter=decision.accepted_filter,
                    order_by=decision.accepted_order,
                    limit=decision.accepted_limit,
                    maximum_rows=self._policy.maximum_rows_per_source if needs_guard else None,
                    properties=PlanProperties(
                        output_fields=projection,
                        logical_id=source.logical_id,
                        ids_are_unique=True,
                        ordering=decision.accepted_order if decision.accepted_order else None,
                        location=remote_location(source.source_name),
                        completeness=ResultCompleteness.EXACT,
                        result_shape=ResultShape.RECORDS,
                        catalog_fingerprint=resolved.query.catalog_fingerprint,
                    ),
                )
            )
        return tuple(scans)

    def _assembly(self, scans: tuple[RemoteScan, ...], resolved: SourceResolvedQuery) -> RecordAssembly:
        anchor, *contributors = scans
        fields = _deduplicate_fields(field for scan in scans for field in scan.projection)
        return RecordAssembly(
            anchor=anchor,
            contributors=tuple(contributors),
            # A contributor scan with a pushed top-level AND conjunct is a
            # candidate-ID restriction as well as an enrichment source.  The
            # executor must retain only anchor records present in that scan.
            # This keeps a non-returned contributor from being mistaken for a
            # logical NULL during the defensive residual evaluation.
            required_contributor_matches=tuple(
                scan.source.source_name for scan in contributors if scan.pushed_filter is not None
            ),
            properties=PlanProperties(
                output_fields=fields,
                logical_id=anchor.properties.logical_id,
                ids_are_unique=True,
                ordering=None,
                location=coordinator_location(),
                completeness=ResultCompleteness.EXACT,
                result_shape=ResultShape.RECORDS,
                catalog_fingerprint=resolved.query.catalog_fingerprint,
            ),
        )


def _source_projection(source: SingleSourceQueryBinding) -> tuple[ResolvedField, ...]:
    """Always include identity; contributor source.fields may not contain it."""

    return _deduplicate_fields((source.logical_id, *source.fields))


def _source_local_filters(resolved: SourceResolvedQuery) -> dict[str, BoundFilterExpression | None]:
    """Split only top-level conjunction leaves into source-owned fragments."""

    predicates = _conjunctive_predicates(resolved.query.where)
    if predicates is None:
        return {source.source_name: None for source in resolved.sources}
    # ``id`` is owned by the configured identity source.  Every participant
    # also has a physical representation of it purely so record assembly can
    # link rows; that must not accidentally change logical field ownership.
    fields_to_source = {
        field.field.name: source.source_name
        for source in resolved.sources
        for field in source.fields
    }
    fields_to_source["id"] = resolved.identity_source.source_name
    grouped: dict[str, list[BoundPredicate]] = {source.source_name: [] for source in resolved.sources}
    for predicate in predicates:
        source_name = fields_to_source.get(predicate.field.name)
        if source_name is not None:
            grouped[source_name].append(predicate)
    return {
        source_name: None
        if not source_predicates
        else source_predicates[0]
        if len(source_predicates) == 1
        else BoundAllExpression(tuple(source_predicates))
        for source_name, source_predicates in grouped.items()
    }


def _conjunctive_predicates(expression: BoundFilterExpression | None) -> tuple[BoundPredicate, ...] | None:
    if expression is None:
        return ()
    if isinstance(expression, BoundPredicate):
        return (expression,)
    if isinstance(expression, BoundAllExpression):
        children = tuple(_conjunctive_predicates(child) for child in expression.expressions)
        if any(child is None for child in children):
            return None
        return tuple(predicate for child in children for predicate in child or ())
    return None


def _effective_limit(query: BoundQuery) -> int:
    assert query.page.first is not None
    maximum = query.constraints.maximum_results
    return min(query.page.first, maximum) if maximum is not None else query.page.first


def _result_projection(query: BoundQuery, scans: tuple[RemoteScan, ...]) -> tuple[ResolvedField, ...]:
    # Retain the identity-source representation for logical ``id`` (and the
    # first declared owner for every other field) instead of letting a
    # contributor's physical ID mapping overwrite the logical property.
    fields: dict[str, ResolvedField] = {}
    for scan in scans:
        for field in scan.projection:
            fields.setdefault(field.field.name, field)
    return tuple(fields[field.name] for field in query.select)


def _field_named(name: str, fields: tuple[ResolvedField, ...]) -> ResolvedField:
    for field in fields:
        if field.field.name == name:
            return field
    raise AssertionError(f"Required logical field '{name}' is absent from plan projection")


def _deduplicate_fields(fields) -> tuple[ResolvedField, ...]:
    result: list[ResolvedField] = []
    seen: set[str] = set()
    for field in fields:
        if field.field.name not in seen:
            result.append(field)
            seen.add(field.field.name)
    return tuple(result)


def _properties_from(properties: PlanProperties, **changes) -> PlanProperties:
    values = {
        "output_fields": properties.output_fields,
        "logical_id": properties.logical_id,
        "ids_are_unique": properties.ids_are_unique,
        "ordering": properties.ordering,
        "location": properties.location,
        "completeness": properties.completeness,
        "result_shape": properties.result_shape,
        "catalog_fingerprint": properties.catalog_fingerprint,
    }
    values.update(changes)
    return PlanProperties(**values)


def _plan_fingerprint(plan: PhysicalPlan) -> str:
    encoded = json.dumps(_plan_shape(plan), sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _plan_shape(plan: PhysicalPlan) -> dict[str, object]:
    if isinstance(plan, RemoteScan):
        return {
            "kind": "remote_scan",
            "source": plan.source.source_name,
            "fields": [field.field.name for field in plan.projection],
            "filter": _filter_shape(plan.pushed_filter),
            "order": [(term.field.name, term.direction.value) for term in plan.order_by],
            "limit": plan.limit,
            "maximum_rows": plan.maximum_rows,
        }
    if isinstance(plan, RecordAssembly):
        return {"kind": "record_assembly", "anchor": _plan_shape(plan.anchor), "contributors": [_plan_shape(item) for item in plan.contributors], "required_contributor_matches": list(plan.required_contributor_matches)}
    if isinstance(plan, CoordinatorFilter):
        return {"kind": "coordinator_filter", "input": _plan_shape(plan.input), "filter": _filter_shape(plan.expression)}
    if isinstance(plan, CoordinatorSortPage):
        return {"kind": "coordinator_sort_page", "input": _plan_shape(plan.input), "order": [(term.field.name, term.direction.value) for term in plan.order_by], "first": plan.first}
    if isinstance(plan, ResultProject):
        return {"kind": "result_project", "input": _plan_shape(plan.input), "fields": [field.field.name for field in plan.projection]}
    raise AssertionError(f"Unknown physical plan: {plan!r}")


def _filter_shape(expression: BoundFilterExpression | None) -> object:
    if expression is None:
        return None
    if isinstance(expression, BoundPredicate):
        return {"field": expression.field.name, "operator": expression.operator.value}
    if isinstance(expression, BoundAllExpression):
        return {"all": [_filter_shape(child) for child in expression.expressions]}
    name = type(expression).__name__.replace("Bound", "").replace("Expression", "").lower()
    child = getattr(expression, "expression", None)
    children = getattr(expression, "expressions", None)
    return {name: _filter_shape(child) if child is not None else [_filter_shape(item) for item in children]}


def _explain_nodes(plan: PhysicalPlan) -> list[PlanExplanationNode]:
    if isinstance(plan, RemoteScan):
        return [
            PlanExplanationNode(
                kind="remote_scan",
                location=plan.source.source_name,
                fields=tuple(field.field.name for field in plan.projection),
                pushed_filter_fields=_filter_fields(plan.pushed_filter),
                ordering=tuple(f"{term.field.name} {term.direction.value}" for term in plan.order_by),
                limit=plan.limit,
            )
        ]
    if isinstance(plan, RecordAssembly):
        return [*_explain_nodes(plan.anchor), *[node for child in plan.contributors for node in _explain_nodes(child)], PlanExplanationNode("record_assembly", "coordinator", tuple(field.field.name for field in plan.properties.output_fields))]
    if isinstance(plan, CoordinatorFilter):
        return [*_explain_nodes(plan.input), PlanExplanationNode("coordinator_filter", "coordinator", tuple(field.field.name for field in plan.properties.output_fields), residual_filter=plan.expression is not None)]
    if isinstance(plan, CoordinatorSortPage):
        return [*_explain_nodes(plan.input), PlanExplanationNode("coordinator_sort_page", "coordinator", tuple(field.field.name for field in plan.properties.output_fields), ordering=tuple(f"{term.field.name} {term.direction.value}" for term in plan.order_by), limit=plan.first)]
    if isinstance(plan, ResultProject):
        return [*_explain_nodes(plan.input), PlanExplanationNode("result_project", "coordinator", tuple(field.field.name for field in plan.projection))]
    raise AssertionError(f"Unknown physical plan: {plan!r}")


def _filter_fields(expression: BoundFilterExpression | None) -> tuple[str, ...]:
    if expression is None:
        return ()
    if isinstance(expression, BoundPredicate):
        return (expression.field.name,)
    if isinstance(expression, BoundAllExpression):
        return tuple(name for child in expression.expressions for name in _filter_fields(child))
    child = getattr(expression, "expression", None)
    children = getattr(expression, "expressions", None)
    return _filter_fields(child) if child is not None else tuple(name for item in children for name in _filter_fields(item))


def _fail(code: ErrorCode, message: str, location: str | None = None) -> None:
    raise QueryError(ErrorDetail(code=code, message=message, retryable=False, location=location))
