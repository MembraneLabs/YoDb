"""What every planning operator shares.

Planning has a fixed relational *spine* (scan -> combine -> filter -> order/page
-> project) and *extension* operators that plug into the slot after filtering
(a semantic filter, graph traversal, ...).  An extension:

1. **claims** the terms of the query it owns, which are removed from the plain
   filter the spine plans;
2. **plans** itself: lists the legal *strategies* given what the sources can do,
   with the fixed-rule default marked;
3. is then costed (the optimizer picks among the strategies' cost variants) and
   built into the physical plan.

Nothing in the planner, the optimizer or the executor names an extension.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ...catalog import SourceKind
from ...operators import OperatorKind
from ...query.models import BoundFilterExpression, BoundQuery
from ...query.resolution import SourceResolvedQuery
from ..capabilities import SourceCapabilities
from ..contracts import PhysicalNode, RemoteScan
from ..optimizer import CostParameters, Variant
from ..policy import PlannerPolicy
from ..registry import SourcePlanningRegistry
from ..statistics import StatisticsService


@dataclass(frozen=True)
class PlanningServices:
    """Everything an operator may consult while planning (read-only)."""

    adapters: SourcePlanningRegistry
    policy: PlannerPolicy
    statistics: StatisticsService | None
    costs: CostParameters

    def capabilities(self, source_kind: SourceKind) -> SourceCapabilities:
        return self.adapters.adapter_for(source_kind).capabilities

    def row_cap(self, source_kind: SourceKind) -> int:
        """Most rows one scan may return: the policy bound, or the source's own if lower."""

        declared = self.capabilities(source_kind).maximum_rows
        bound = self.policy.maximum_rows_per_source
        return bound if declared is None else min(bound, declared)


def effective_limit(query: BoundQuery) -> int:
    """The page size actually in force (the page, bounded by ``maximum_results``)."""

    assert query.page.first is not None
    maximum = query.constraints.maximum_results
    return min(query.page.first, maximum) if maximum is not None else query.page.first


@dataclass(frozen=True)
class Claim:
    """The terms an extension operator owns, and the filter that remains for the spine."""

    terms: tuple[object, ...]
    remaining: BoundFilterExpression | None


@dataclass(frozen=True)
class Strategy:
    """One legal way to run an extension operator on this query.

    ``variant`` is how the cost model sees it.  ``prepare`` adjusts the scans
    before they are assembled (e.g. a ranked read); ``build`` wraps the plan so
    far in the operator's node, given the notes explaining the choice.
    """

    variant: Variant
    build: Callable[[PhysicalNode, tuple[str, ...]], PhysicalNode]
    prepare: Callable[[tuple[RemoteScan, ...]], tuple[RemoteScan, ...]] = lambda scans: scans


@dataclass(frozen=True)
class ExtensionPlan:
    """The strategies an extension offers for this query."""

    operator: OperatorKind
    strategies: tuple[Strategy, ...]
    default: Strategy                       # what the fixed rules choose (always legal)
    # Notes for the final choice: ``chosen`` is the cost-based pick, or None when the
    # fixed-rule default stands (e.g. the optimizer declined).
    notes_for: Callable[[Strategy | None], tuple[str, ...]] = lambda chosen: ()

    def strategy_for(self, variant: Variant | None) -> Strategy | None:
        return next((s for s in self.strategies if s.variant is variant), None)

    @property
    def variants(self) -> tuple[Variant, ...]:
        return tuple(s.variant for s in self.strategies)


class ExtensionOperator(Protocol):
    """An operator beyond the relational spine."""

    kind: OperatorKind

    def claim(self, where: BoundFilterExpression | None) -> Claim | None:
        """The terms of ``where`` this operator owns, or None if it owns none."""

    def plan(
        self,
        claim: Claim,
        core: SourceResolvedQuery,
        scans: "ScanPlanLike",
        query: BoundQuery,
    ) -> ExtensionPlan:
        """The legal strategies for the claimed terms (raise a QueryError if none can run)."""


class PlanningExtension(Protocol):
    """An extension's planning half: builds its operator from the planner's services."""

    def planning_operator(self, services: PlanningServices) -> ExtensionOperator: ...


class ScanPlanLike(Protocol):
    scans: Sequence[RemoteScan]
    fully_pushed: bool
    page_pushed: bool
