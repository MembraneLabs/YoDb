"""A planning adapter implemented entirely from a ``SourceCapabilities`` description.

Adding a database means declaring its capabilities here, not re-implementing
pushdown.  The adapter accepts a fragment only if the declared capabilities
cover every part of it, and reports why anything was refused.
"""

from __future__ import annotations

from ..catalog import SourceKind
from ..query.models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    BoundSemanticPredicate,
)
from ..query.resolution import SingleSourceQueryBinding
from .capabilities import BooleanOperator, SourceCapabilities
from .contracts import PushdownDecision, SourceOperationRequest, SourcePlanningAdapter


class CapabilityPlanningAdapter(SourcePlanningAdapter):
    """Decide pushdown from declared capabilities (see :class:`SourceCapabilities`)."""

    def __init__(self, capabilities: SourceCapabilities) -> None:
        self._capabilities = capabilities

    @property
    def source_kind(self) -> SourceKind:
        return self._capabilities.source_kind

    @property
    def capabilities(self) -> SourceCapabilities:
        return self._capabilities

    def plan_remote_scan(
        self,
        source: SingleSourceQueryBinding,
        requested: SourceOperationRequest,
    ) -> PushdownDecision:
        caps = self._capabilities
        if source.source_kind is not caps.source_kind:
            raise ValueError(f"{caps.source_kind.value} planning requires a {caps.source_kind.value} source binding")
        reasons: list[str] = []
        names = {field.field.name for field in (*source.fields, source.logical_id)}
        accepted_filter = requested.filter if self._accepts(requested.filter, names, reasons) else None
        residual = None if accepted_filter is requested.filter else requested.filter

        # A remote ORDER/LIMIT is only safe after every result-affecting
        # predicate was accepted; otherwise it could discard a row the
        # coordinator residual would have retained.  A limit is only meaningful
        # together with the order that defines it.
        accepted_order: tuple = ()
        accepted_limit = None
        if requested.complete_result and residual is None:
            order_ok = caps.supports_order and all(
                term.field.spec.type in caps.orderable_types for term in requested.order_by
            )
            if order_ok:
                accepted_order = requested.order_by
                if caps.supports_limit:
                    accepted_limit = requested.limit
            elif requested.order_by:
                reasons.append("the source cannot order by the requested fields")
        return PushdownDecision(
            accepted_filter=accepted_filter,
            residual_filter=residual,
            accepted_projection=requested.projection,
            accepted_order=accepted_order,
            accepted_limit=accepted_limit,
            limit_is_guaranteed=accepted_limit is not None,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def _accepts(
        self,
        expression: BoundFilterExpression | None,
        names: set[str],
        reasons: list[str],
    ) -> bool:
        caps = self._capabilities
        if expression is None:
            return True
        if isinstance(expression, BoundPredicate):
            if expression.field.name not in names:
                reasons.append("a predicate references a field the source does not own")
                return False
            if expression.operator not in caps.filter_operators:
                reasons.append(f"the source cannot filter with '{expression.operator.value}'")
                return False
            if expression.field.spec.type not in caps.filterable_types:
                reasons.append(f"the source cannot filter on type '{expression.field.spec.type.value}'")
                return False
            return True
        if isinstance(expression, BoundSemanticPredicate):
            reasons.append("a semantic condition is not a source filter")
            return False
        if isinstance(expression, (BoundAllExpression, BoundAnyExpression)):
            operator = BooleanOperator.ALL if isinstance(expression, BoundAllExpression) else BooleanOperator.ANY
            if operator not in caps.boolean_operators:
                reasons.append(f"the source cannot combine terms with '{operator.value}'")
                return False
            return all([self._accepts(child, names, reasons) for child in expression.expressions])
        if isinstance(expression, BoundNotExpression):
            if BooleanOperator.NOT not in caps.boolean_operators:
                reasons.append("the source cannot negate terms with 'not'")
                return False
            return self._accepts(expression.expression, names, reasons)
        raise AssertionError(f"Unknown bound expression: {expression!r}")
