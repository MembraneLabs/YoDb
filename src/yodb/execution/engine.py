"""Central source-agnostic execution path for supported YoDb queries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..compilation import QueryCompilerRegistry
from ..planning import (
    FederatedPhysicalPlanner,
    PostgresPlanningAdapter,
    SemanticPolicy,
    SourcePlanningRegistry,
)
from ..semantic import SemanticQueryReport, SemanticRuntime
from ..query import QueryValidationPolicy, bind_query, parse_query, resolve_query_sources
from .contracts import ActiveCatalogProvider, QueryExecutionResult
from .federated import ExecutionTrace, FederatedPlanExecutor
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
        semantic: SemanticRuntime | None = None,
        semantic_policy: SemanticPolicy | None = None,
    ) -> None:
        self._catalog_runtime = catalog_runtime
        self._compilers = compilers
        self._executors = executors
        self._validation_policy = validation_policy
        embedder = semantic.embedder if semantic is not None else None
        # Unless told otherwise, the planner learns which embedding space the
        # configured provider produces from that provider.
        policy = semantic_policy or SemanticPolicy(
            embedder=None if embedder is None else embedder.info,
            embedder_dimensions=None if embedder is None else embedder.dimensions,
        )
        self._planner = planner or FederatedPhysicalPlanner(
            SourcePlanningRegistry([PostgresPlanningAdapter()]), semantic=policy
        )
        self._plan_executor = FederatedPlanExecutor(compilers, executors, semantic=semantic)

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
        report = None
        if trace.semantic_stats is not None:
            returned = {row["id"] for row in rows}
            report = SemanticQueryReport(
                stats=trace.semantic_stats,
                records={key: meta for key, meta in trace.semantic_records.items() if key in returned},
            )
        return QueryExecutionResult.from_rows(
            rows,
            query_fingerprint=bound.query_fingerprint,
            catalog_fingerprint=bound.catalog_fingerprint,
            semantic=report,
        )

    def explain(self, raw_query: Mapping[str, Any]):
        """Return a redacted structural physical-plan explanation without executing it."""

        active = self._catalog_runtime.require_active()
        request = parse_query(raw_query)
        bound = bind_query(request, active, policy=self._validation_policy)
        resolved = resolve_query_sources(bound, active)
        return self._planner.plan(resolved).explain
