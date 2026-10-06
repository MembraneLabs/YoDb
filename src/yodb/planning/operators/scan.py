"""SCAN (and the filter/order/limit pushdown negotiation): one read per source."""

from __future__ import annotations

from dataclasses import dataclass

from ...errors import ErrorCode, ErrorDetail, QueryError
from ...operators import OperatorKind
from ...query.models import BoundAllExpression, BoundFilterExpression
from ...query.resolution import QuerySourceShape, ResolvedField, SingleSourceQueryBinding, SourceResolvedQuery
from ..contracts import (
    PlanProperties,
    RemoteScan,
    ResultCompleteness,
    ResultShape,
    SourceOperationRequest,
    remote_location,
)
from ..expressions import filter_fields, holds_when_every_field_is_null, top_level_conjuncts
from .base import PlanningServices, effective_limit


@dataclass(frozen=True)
class ScanPlan:
    """The reads, and how much of the rest of the query the sources already enforce."""

    scans: tuple[RemoteScan, ...]
    # Every filter term was accepted by its owning source (so no coordinator filter is needed).
    fully_pushed: bool
    # A single source enforced the exact order and page (so no coordinator sort is needed).
    page_pushed: bool


class ScanOperator:
    """Negotiates, per source, which filter/order/limit it will enforce, and builds the reads."""

    kind = OperatorKind.SCAN

    def __init__(self, services: PlanningServices) -> None:
        self._services = services

    def plan(self, resolved: SourceResolvedQuery, *, allow_complete: bool = True) -> ScanPlan:
        """Build one read per source.

        A filter is fully pushed when the single source accepted the whole
        expression, or when it is a pure conjunction whose every leaf was accepted
        by its owning source (a contributor leaf also makes that source a required
        match, so assembly drops anchor rows that fail it).  Otherwise the
        coordinator must evaluate the original expression.
        """

        services = self._services
        source_filters = source_local_filters(resolved)
        complete = resolved.shape is QuerySourceShape.SINGLE_SOURCE and allow_complete
        total_conjuncts = top_level_conjuncts(resolved.query.where)
        pushed_conjuncts = 0
        every_source_accepted = True
        page_pushed = False
        scans = []
        for source in resolved.sources:
            projection = source_projection(source)
            requested = SourceOperationRequest(
                projection=projection,
                filter=resolved.query.where if complete else source_filters.get(source.source_name),
                order_by=resolved.query.order_by if complete else (),
                limit=effective_limit(resolved.query) if complete else None,
                complete_result=complete,
            )
            decision = services.adapters.adapter_for(source.source_kind).plan_remote_scan(source, requested)
            if decision.accepted_projection != projection:
                raise QueryError(
                    ErrorDetail(
                        code=ErrorCode.SOURCE_CAPABILITY_UNAVAILABLE,
                        message="The source planning adapter cannot provide the mandatory logical ID and projection.",
                        retryable=False,
                        location=source.source_name,
                    )
                )
            if requested.filter is not None:
                if decision.accepted_filter is not requested.filter:
                    every_source_accepted = False
                else:
                    pushed_conjuncts += len(top_level_conjuncts(requested.filter))
            if complete:
                page_pushed = (
                    decision.accepted_filter is requested.filter
                    and decision.accepted_order == requested.order_by
                    and decision.accepted_limit == requested.limit
                )
            caps = services.capabilities(source.source_kind)
            needs_guard = not complete or decision.accepted_limit != requested.limit
            scans.append(
                RemoteScan(
                    source=source,
                    projection=projection,
                    pushed_filter=decision.accepted_filter,
                    order_by=decision.accepted_order,
                    limit=decision.accepted_limit,
                    maximum_rows=services.row_cap(source.source_kind) if needs_guard else None,
                    key_lookup_limit=None if caps.key_lookup is None else caps.key_lookup.maximum_keys,
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
        fully_pushed = (
            every_source_accepted if complete else every_source_accepted and pushed_conjuncts == len(total_conjuncts)
        )
        return ScanPlan(tuple(scans), fully_pushed, page_pushed)


def source_projection(source: SingleSourceQueryBinding) -> tuple[ResolvedField, ...]:
    """Always include identity; contributor source.fields may not contain it."""

    return deduplicate_fields((source.logical_id, *source.fields))


def source_local_filters(resolved: SourceResolvedQuery) -> dict[str, BoundFilterExpression | None]:
    """Give each source the conditions only it can decide.

    The filter is split into the conditions it ANDs together.  A condition whose fields all belong
    to one source is that source's: a plain comparison, or a whole ``any``/``not`` over its fields.
    A condition that mixes sources stays with the coordinator.

    Two guards keep results unchanged.  A contributor row that is absent enriches as all-NULL, so
    a condition that could be TRUE of all-NULL fields (``IS NULL``, or an ``any`` containing it) is
    never pushed to a contributor: pushing it would hide contributor rows whose value is non-null
    and make them look NULL after assembly.  The identity source always has a row, so anything on
    its fields may be pushed.
    """

    # ``id`` is owned by the configured identity source.  Every participant also has a physical
    # representation of it purely so record assembly can link rows; that must not accidentally
    # change logical field ownership.
    fields_to_source = {
        field.field.name: source.source_name for source in resolved.sources for field in source.fields
    }
    fields_to_source["id"] = resolved.identity_source.source_name
    identity = resolved.identity_source.source_name
    grouped: dict[str, list[BoundFilterExpression]] = {source.source_name: [] for source in resolved.sources}
    for conjunct in top_level_conjuncts(resolved.query.where):
        owners = {fields_to_source.get(name) for name in filter_fields(conjunct)}
        if len(owners) != 1 or None in owners:
            continue
        (owner,) = owners
        if owner != identity and holds_when_every_field_is_null(conjunct):
            continue
        grouped[owner].append(conjunct)
    return {
        source_name: None if not parts else parts[0] if len(parts) == 1 else BoundAllExpression(tuple(parts))
        for source_name, parts in grouped.items()
    }


def deduplicate_fields(fields) -> tuple[ResolvedField, ...]:
    result: list[ResolvedField] = []
    seen: set[str] = set()
    for field in fields:
        if field.field.name not in seen:
            result.append(field)
            seen.add(field.field.name)
    return tuple(result)
