"""Build one executable physical plan from a resolved logical query.

The planner only orchestrates.  Each step belongs to an operator
(``planning/operators``); the planner calls them in order and assembles the plan:

1. extension operators *claim* the terms of the query they own;
2. the scan operator negotiates what each source enforces (filter/order/limit);
3. each extension *plans* itself: its legal strategies given those sources;
4. the combine operator decides the read order and which strategies are cheapest
   (cost-based when statistics allow, otherwise the fixed rules, with the reason);
5. the plan is built: reads, combine, filter, extension nodes, order/page, project.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from ..errors import ErrorCode, ErrorDetail, QueryError
from ..query.extensions import extension_terms
from ..query.resolution import QuerySourceShape, SourceResolvedQuery
from .contracts import PhysicalNode, PlanExplanation, PlannedQuery, explain_plan, plan_fingerprint
from .operators import (
    CombineDecision,
    CombineOperator,
    FilterOperator,
    OrderPageOperator,
    PlanningExtension,
    PlanningServices,
    ProjectOperator,
    ScanOperator,
)
from .optimizer import CostParameters
from .policy import PlannerPolicy
from .registry import SourcePlanningRegistry
from .statistics import StatisticsService

__all__ = ["FederatedPhysicalPlanner", "PlannerPolicy"]


class FederatedPhysicalPlanner:
    """Create a correct, executable plan; a cheaper one when statistics allow."""

    def __init__(
        self,
        adapters: SourcePlanningRegistry,
        *,
        policy: PlannerPolicy = PlannerPolicy(),
        statistics: StatisticsService | None = None,
        costs: CostParameters = CostParameters(),
        extensions: Sequence[PlanningExtension] = (),
    ) -> None:
        services = PlanningServices(adapters, policy, statistics, costs)
        self._scan = ScanOperator(services)
        self._combine = CombineOperator(services)
        self._filter = FilterOperator()
        self._order_page = OrderPageOperator(services)
        self._project = ProjectOperator()
        # Extension operators plug into the slot after filtering.
        self._extensions = tuple(extension.planning_operator(services) for extension in extensions)

    def plan(self, resolved: SourceResolvedQuery) -> PlannedQuery:
        query = resolved.query
        if query.page.after is not None:
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.QUERY_FEATURE_NOT_SUPPORTED,
                    message="Cursor execution is unavailable until signed cursor verification is implemented.",
                    retryable=False,
                    location="page.after",
                )
            )
        # 1. Extensions claim their terms; the spine plans everything else exactly as usual.
        core, claims = resolved, []
        for extension in self._extensions:
            claim = extension.claim(core.query.where)
            if claim is not None:
                claims.append((extension, claim))
                core = replace(core, query=replace(core.query, where=claim.remaining))
        unclaimed = extension_terms(core.query.where)
        if unclaimed:
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.QUERY_FEATURE_NOT_SUPPORTED,
                    message=f"No planning operator is registered for the {type(unclaimed[0]).__name__} term.",
                    retryable=False,
                    location="where",
                )
            )
        # 2. What each source will enforce.
        scan_plan = self._scan.plan(core, allow_complete=not claims)
        # 3. Each extension's legal strategies.
        extension_plans = tuple(extension.plan(claim, core, scan_plan, query) for extension, claim in claims)
        # 4. Read order and strategy choice: cost-based, or the fixed rules and why.
        multi_source = resolved.shape is QuerySourceShape.MULTI_SOURCE
        decision = (
            self._combine.decide(scan_plan, extension_plans, query)
            if claims or multi_source
            else CombineDecision((), (), ())
        )
        chosen = [
            (ext_plan.default if pick is None else pick, ext_plan.notes_for(pick))
            for ext_plan, pick in zip(extension_plans, decision.chosen)
        ]

        # 5. Build the plan.
        scans = scan_plan.scans
        for strategy, _ in chosen:
            scans = strategy.prepare(scans)
        node: PhysicalNode = scans[0] if resolved.shape is QuerySourceShape.SINGLE_SOURCE else self._combine.build(scans, core, decision.schedule)
        node = self._filter.build(node, core.query.where, fully_pushed=scan_plan.fully_pushed)
        for strategy, notes in chosen:
            node = strategy.build(node, notes)
        node = self._order_page.build(node, query, scans, page_pushed=scan_plan.page_pushed)
        node = self._project.build(node, query, scans)

        fingerprint = plan_fingerprint(node)
        explanation = PlanExplanation(
            plan_kind="single_source" if resolved.shape is QuerySourceShape.SINGLE_SOURCE else "in_memory_record_assembly",
            catalog_fingerprint=query.catalog_fingerprint,
            plan_fingerprint=fingerprint,
            nodes=explain_plan(node),
            optimizer=decision.notes,
        )
        return PlannedQuery(
            query=query,
            resolved=resolved,
            plan=node,
            catalog_fingerprint=query.catalog_fingerprint,
            query_fingerprint=query.query_fingerprint,
            plan_fingerprint=fingerprint,
            explain=explanation,
        )
