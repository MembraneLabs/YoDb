"""PostgreSQL semantic pushdown decisions for planner-produced fragments."""

from __future__ import annotations

from ..catalog import SourceKind
from ..query.models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    ComparisonOperator,
)
from ..query.resolution import SingleSourceQueryBinding
from .contracts import PushdownDecision, SourceOperationRequest, SourcePlanningAdapter


_OPERATORS = frozenset(
    {
        ComparisonOperator.EQ,
        ComparisonOperator.NE,
        ComparisonOperator.IN,
        ComparisonOperator.NOT_IN,
        ComparisonOperator.IS_NULL,
        ComparisonOperator.IS_NOT_NULL,
        ComparisonOperator.GT,
        ComparisonOperator.GTE,
        ComparisonOperator.LT,
        ComparisonOperator.LTE,
    }
)


class PostgresPlanningAdapter(SourcePlanningAdapter):
    """Declare the portable logical operations the V0.1 compiler preserves."""

    source_kind = SourceKind.POSTGRES
    supports_key_lookup = True

    def plan_remote_scan(
        self,
        source: SingleSourceQueryBinding,
        requested: SourceOperationRequest,
    ) -> PushdownDecision:
        if source.source_kind is not SourceKind.POSTGRES:
            raise ValueError("PostgreSQL planning requires a postgres source binding")
        accepted_filter = requested.filter if _supported_by_source(requested.filter, source) else None
        residual = None if accepted_filter is requested.filter else requested.filter
        # A remote ORDER/LIMIT is only safe after every result-affecting
        # predicate was accepted.  Otherwise it could discard a row that the
        # coordinator residual would have retained.
        accepted_order = requested.order_by if requested.complete_result and residual is None else ()
        accepted_limit = requested.limit if requested.complete_result and residual is None else None
        return PushdownDecision(
            accepted_filter=accepted_filter,
            residual_filter=residual,
            accepted_projection=requested.projection,
            accepted_order=accepted_order,
            accepted_limit=accepted_limit,
            limit_is_guaranteed=accepted_limit is not None,
            reasons=() if residual is None else ("one or more requested predicates lack V0.1 PostgreSQL semantics",),
        )


def _supported_by_source(
    expression: BoundFilterExpression | None,
    source: SingleSourceQueryBinding,
) -> bool:
    if expression is None:
        return True
    names = {field.field.name for field in (*source.fields, source.logical_id)}
    if isinstance(expression, BoundPredicate):
        return expression.field.name in names and expression.operator in _OPERATORS
    if isinstance(expression, (BoundAllExpression, BoundAnyExpression)):
        return all(_supported_by_source(child, source) for child in expression.expressions)
    if isinstance(expression, BoundNotExpression):
        return _supported_by_source(expression.expression, source)
    raise AssertionError(f"Unknown bound expression: {expression!r}")
