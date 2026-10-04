"""FILTER: the coordinator evaluates whatever the sources did not enforce."""

from __future__ import annotations

from ...operators import OperatorKind
from ...query.models import BoundFilterExpression
from ..contracts import CoordinatorFilter, PhysicalNode, coordinator_location, properties_from


class FilterOperator:
    kind = OperatorKind.FILTER

    def build(self, node: PhysicalNode, expression: BoundFilterExpression | None, *, fully_pushed: bool) -> PhysicalNode:
        """Add a coordinator filter unless every term was already enforced by its source."""

        if fully_pushed:
            return node
        return CoordinatorFilter(
            input=node,
            expression=expression,
            properties=properties_from(node.properties, location=coordinator_location()),
        )
