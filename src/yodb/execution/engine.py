"""Central source-agnostic execution path for supported YoDb queries."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..compilation import QueryCompilerRegistry
from ..planning import InMemoryAssemblyPlan, PhysicalQueryPlanner, SingleSourcePlan
from ..query import QueryValidationPolicy, bind_query, parse_query, resolve_query_sources
from .contracts import ActiveCatalogProvider, QueryExecutionResult
from .in_memory import assemble
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
        planner: PhysicalQueryPlanner | None = None,
    ) -> None:
        self._catalog_runtime = catalog_runtime
        self._compilers = compilers
        self._executors = executors
        self._validation_policy = validation_policy
        self._planner = planner or PhysicalQueryPlanner()

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
        plan = self._planner.plan(resolved)
        rows = self._execute_plan(plan, timeout_seconds=timeout_seconds)
        return QueryExecutionResult.from_rows(
            rows,
            query_fingerprint=bound.query_fingerprint,
            catalog_fingerprint=bound.catalog_fingerprint,
        )

    def _execute_plan(
        self,
        plan: SingleSourcePlan | InMemoryAssemblyPlan,
        *,
        timeout_seconds: float | None,
    ) -> tuple[Mapping[str, object], ...]:
        if isinstance(plan, SingleSourcePlan):
            return self._execute_scan(plan.scan, timeout_seconds=timeout_seconds)
        scans = tuple(
            self._execute_scan(scan, timeout_seconds=timeout_seconds) for scan in plan.scans
        )
        return assemble(plan, scans)

    def _execute_scan(self, scan: Any, *, timeout_seconds: float | None) -> tuple[Mapping[str, object], ...]:
        compiled = self._compilers.adapter_for(scan.source.source_kind).compile(scan)
        return self._executors.adapter_for(compiled.source_kind).execute(
            compiled, timeout_seconds=timeout_seconds
        )
