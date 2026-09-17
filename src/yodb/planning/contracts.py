"""Small physical-plan model for V0.1 source scans and record assembly."""

from __future__ import annotations

from dataclasses import dataclass

from ..query.models import BoundFilterExpression, BoundOrderTerm, BoundQuery
from ..query.resolution import ResolvedField, SingleSourceQueryBinding


@dataclass(frozen=True)
class SourceScanPlan:
    """One source-local read that a backend compiler can turn into native work."""

    source: SingleSourceQueryBinding
    projection: tuple[ResolvedField, ...]
    where: BoundFilterExpression | None
    order_by: tuple[BoundOrderTerm, ...]
    limit: int | None


@dataclass(frozen=True)
class SingleSourcePlan:
    """A one-stage plan whose source may preserve the entire query meaning."""

    scan: SourceScanPlan
    query: BoundQuery


@dataclass(frozen=True)
class InMemoryAssemblyPlan:
    """Several unoptimized source scans joined by their declared logical ID.

    The identity source anchors records. Contributor rows without an anchor are
    ignored; missing contributor values remain ``None``. The complete Boolean
    filter, ordering, and page are intentionally evaluated after assembly.
    """

    scans: tuple[SourceScanPlan, ...]
    query: BoundQuery
    maximum_rows_per_source: int


PhysicalQueryPlan = SingleSourcePlan | InMemoryAssemblyPlan
