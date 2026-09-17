"""Build correct, unoptimized physical plans from resolved source participation."""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import ErrorCode, ErrorDetail, QueryError
from ..query.resolution import QuerySourceShape, ResolvedField, SourceResolvedQuery
from .contracts import InMemoryAssemblyPlan, PhysicalQueryPlan, SingleSourcePlan, SourceScanPlan


@dataclass(frozen=True)
class InMemoryPlanPolicy:
    """Non-negotiable transfer bound for unoptimized federated assembly."""

    maximum_rows_per_source: int = 10_000

    def __post_init__(self) -> None:
        if self.maximum_rows_per_source <= 0:
            raise ValueError("maximum_rows_per_source must be positive")


class PhysicalQueryPlanner:
    """Turn source resolution into a minimal, backend-neutral execution plan.

    It deliberately makes no cost-based choices, no predicate pushdown for
    multi-source work, and no parallel scheduling decisions in this increment.
    """

    def __init__(self, *, policy: InMemoryPlanPolicy = InMemoryPlanPolicy()) -> None:
        self._policy = policy

    def plan(self, query: SourceResolvedQuery) -> PhysicalQueryPlan:
        if query.query.page.after is not None:
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.QUERY_COMPILATION_UNSUPPORTED,
                    message="Cursor pagination is not compiled until signed cursor verification is implemented.",
                    retryable=False,
                    location="page.after",
                )
            )
        if query.shape is QuerySourceShape.SINGLE_SOURCE:
            scan = query.identity_source
            return SingleSourcePlan(
                scan=SourceScanPlan(
                    source=scan,
                    projection=_projection(query, scan.fields, scan.logical_id),
                    where=query.query.where,
                    order_by=query.query.order_by,
                    limit=_effective_limit(query),
                ),
                query=query.query,
            )

        return InMemoryAssemblyPlan(
            scans=tuple(
                SourceScanPlan(
                    source=source,
                    projection=_all_source_fields(source.fields, source.logical_id),
                    where=None,
                    order_by=(),
                    # Fetch one extra row so the coordinator can distinguish a
                    # safe complete scan from one that crossed its hard budget.
                    limit=self._policy.maximum_rows_per_source + 1,
                )
                for source in query.sources
            ),
            query=query.query,
            maximum_rows_per_source=self._policy.maximum_rows_per_source,
        )


def _projection(
    query: SourceResolvedQuery,
    fields: tuple[ResolvedField, ...],
    logical_id: ResolvedField,
) -> tuple[ResolvedField, ...]:
    by_name = {field.field.name: field for field in fields}
    by_name.setdefault(logical_id.field.name, logical_id)
    return tuple(by_name[field.name] for field in query.query.select)


def _all_source_fields(
    fields: tuple[ResolvedField, ...], logical_id: ResolvedField
) -> tuple[ResolvedField, ...]:
    by_name = {field.field.name: field for field in fields}
    by_name.setdefault(logical_id.field.name, logical_id)
    return tuple(by_name[name] for name in sorted(by_name))


def _effective_limit(query: SourceResolvedQuery) -> int:
    page_limit = query.query.page.first
    assert page_limit is not None
    requested_maximum = query.query.constraints.maximum_results
    return min(page_limit, requested_maximum) if requested_maximum is not None else page_limit
