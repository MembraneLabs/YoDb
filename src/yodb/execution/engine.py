"""Central source-agnostic execution path for supported YoDb queries."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..compilation import QueryCompilerRegistry
from ..planning import (
    CostParameters,
    FederatedPhysicalPlanner,
    PostgresPlanningAdapter,
    SourcePlanningRegistry,
    StatisticsService,
)
from ..query import QueryValidationPolicy, bind_query, parse_query, resolve_query_sources
from .contracts import ActiveCatalogProvider, QueryExecutionResult
from .federated import ExecutionTrace, FederatedPlanExecutor
from .operators import ExecutionExtension
from .registry import QueryExecutionAdapterRegistry


class QueryExecutionEngine:
    """Parse, bind, resolve, compile, and execute one logical query.

    This is the central execution boundary for V0.1.  It owns no driver,
    source credentials, pool, SQL, or backend-specific result type.  Those
    details remain behind adapters selected only from validated catalog data.
    """

    def __init__(
        self,
        catalog_runtime: ActiveCatalogProvider,
        compilers: QueryCompilerRegistry,
        executors: QueryExecutionAdapterRegistry,
        *,
        validation_policy: QueryValidationPolicy = QueryValidationPolicy(),
        planner: FederatedPhysicalPlanner | None = None,
        extensions: Sequence[ExecutionExtension] = (),
        statistics: StatisticsService | None = None,
        cost_parameters: CostParameters | None = None,
    ) -> None:
        self._catalog_runtime = catalog_runtime
        self._compilers = compilers
        self._executors = executors
        self._validation_policy = validation_policy
        self._statistics = statistics
        self._planner = planner or FederatedPhysicalPlanner(
            SourcePlanningRegistry([PostgresPlanningAdapter()]),
            extensions=tuple(extensions),
            statistics=statistics,
            costs=cost_parameters or CostParameters(),
        )
        self._plan_executor = FederatedPlanExecutor(compilers, executors, extensions=extensions)

    def execute(
        self,
        raw_query: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> QueryExecutionResult:
        """Execute one supported structured logical query against the active catalog."""

        active = self._catalog_runtime.require_active()
        request = parse_query(raw_query)
        bound = bind_query(request, active, policy=self._validation_policy)
        resolved = resolve_query_sources(bound, active)
        planned = self._planner.plan(resolved)
        trace = ExecutionTrace()
        rows = self._plan_executor.execute(planned.plan, timeout_seconds=timeout_seconds, trace=trace)
        if self._statistics is not None:
            # Teach the estimator what unrestricted scans really returned.
            for actual in trace.scan_actuals:
                self._statistics.observe(actual.scan.source, actual.scan.pushed_filter, actual.rows)
        returned = {row["id"] for row in rows}
        reports = {
            name: report.restricted_to(returned) if hasattr(report, "restricted_to") else report
            for name, report in trace.reports.items()
        }
        return QueryExecutionResult.from_rows(
            rows,
            query_fingerprint=bound.query_fingerprint,
            catalog_fingerprint=bound.catalog_fingerprint,
            reports=reports,
        )

    def explain(self, raw_query: Mapping[str, Any]):
        """Return a redacted structural physical-plan explanation without executing it."""

        active = self._catalog_runtime.require_active()
        request = parse_query(raw_query)
        bound = bind_query(request, active, policy=self._validation_policy)
        resolved = resolve_query_sources(bound, active)
        return self._planner.plan(resolved).explain

