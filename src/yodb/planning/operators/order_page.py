"""ORDER and LIMIT: the coordinator orders and pages unless one source already did."""

from __future__ import annotations

from ...catalog import LogicalType
from ...operators import OperatorKind
from ...query.models import BoundQuery
from ..capabilities import TextOrdering
from ..contracts import CoordinatorSortPage, PhysicalNode, RemoteScan, coordinator_location, properties_from
from .base import PlanningServices, effective_limit


class OrderPageOperator:
    kind = OperatorKind.ORDER

    def __init__(self, services: PlanningServices) -> None:
        self._services = services

    def build(self, node: PhysicalNode, query: BoundQuery, scans: tuple[RemoteScan, ...], *, page_pushed: bool) -> PhysicalNode:
        """Add a coordinator sort/page, unless the one source enforced the exact order and page.

        Re-sorting there would substitute Python's string ordering for the
        source's collation.
        """

        if page_pushed:
            return node
        return CoordinatorSortPage(
            input=node,
            order_by=query.order_by,
            first=effective_limit(query),
            notes=self.ordering_notes(query, scans),
            properties=properties_from(node.properties, ordering=query.order_by, location=coordinator_location()),
        )

    def ordering_notes(self, query: BoundQuery, scans: tuple[RemoteScan, ...]) -> tuple[str, ...]:
        """Flag text ordering done by YoDb that the owning source would collate differently."""

        if len(scans) < 2:
            return ()
        owner = {f.field.name: f.source_name for scan in scans for f in scan.projection if f.field.name != "id"}
        kind_of = {scan.source.source_name: scan.source.source_kind for scan in scans}
        differing = sorted(
            {
                owner[term.field.name]
                for term in query.order_by
                if term.field.spec.type in {LogicalType.STRING, LogicalType.TEXT}
                and term.field.name in owner
                and self._services.capabilities(kind_of[owner[term.field.name]]).text_ordering is TextOrdering.SOURCE_DEFINED
            }
        )
        if not differing:
            return ()
        return (
            "text is ordered by YoDb in code-point order; "
            f"the owning source's collation differs for: {', '.join(differing)}",
        )
