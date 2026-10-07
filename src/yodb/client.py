"""The front door: open a catalog and ask questions.

    import yodb

    with yodb.connect("catalog/") as db:                       # connections from YODB_CONN_<REF> variables
        result = db.query({"from": {"dataset": "customer"}, "select": ["name"], "page": {"first": 10}})
        print(db.explain(query).nodes)

``connect`` builds everything a query needs from one catalog directory: the
PostgreSQL pool (read-only sessions), the catalog runtime (which inspects the real
sources and refuses to activate a catalog that does not match them), the planner
with statistics, and the executor.  Anything else can still be assembled by hand
from the lower-level pieces; this is only the short path.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
from typing import Any

from .catalog import Catalog, SourceKind
from .compilation import PostgresQueryCompiler, QueryCompilerRegistry
from .connections import (
    EnvPostgresConnectionResolver,
    MappingPostgresConnectionResolver,
    PostgresConnectionAdapter,
    PostgresConnectionReferenceResolver,
    PostgresConnectionSettings,
)
from .errors import CatalogRuntimeError, ErrorCode, ErrorDetail, QueryError
from .execution import (
    PostgresQueryExecutionAdapter,
    QueryExecutionAdapterRegistry,
    QueryExecutionEngine,
    QueryExecutionResult,
)
from .execution.operators import FederatedExecutionPolicy
from .inspection import (
    InspectionAdapterBinding,
    PostgresCatalogValidator,
    PostgresSourceInspector,
    SourceInspectionRegistry,
)
from .planning import (
    FederatedPhysicalPlanner,
    ObservationStore,
    JoinPolicy,
    PlannerPolicy,
    PlanExplanation,
    PostgresPlanningAdapter,
    PostgresStatisticsProvider,
    SourcePlanningRegistry,
    StatisticsService,
)
from .runtime import CatalogRefreshResult, InMemoryCatalogRuntime
from .semantic import SemanticExtension, SemanticRuntime

Connections = Mapping[str, "str | PostgresConnectionSettings"] | PostgresConnectionReferenceResolver | None


class YoDb:
    """An open, validated catalog and the engine that answers queries against it."""

    def __init__(
        self, runtime: InMemoryCatalogRuntime, engine: QueryExecutionEngine, *, close=None, semantic: bool = False
    ) -> None:
        self._runtime = runtime
        self._engine = engine
        self._close = close
        self._semantic = semantic

    @classmethod
    def connect(
        cls,
        catalog: str | Path,
        connections: Connections = None,
        *,
        semantic: SemanticExtension | SemanticRuntime | None = None,
        statistics: bool = True,
        pool_size: int = 10,
        acquire_timeout_seconds: float = 30.0,
        statement_timeout_seconds: float | None = 60.0,
        planner_policy: PlannerPolicy = PlannerPolicy(),
        execution_policy: FederatedExecutionPolicy = FederatedExecutionPolicy(),
        join_policy: JoinPolicy = JoinPolicy(),
        pool_factory=None,
    ) -> "YoDb":
        """Load ``catalog`` (a directory of the three YAML files), inspect every source, and open it.

        ``connections`` maps each catalog ``connection_ref`` to a libpq conninfo string, or is a
        resolver object; left out, each reference is read from ``YODB_CONN_<REF>``.  Raises a
        :class:`CatalogRuntimeError` listing what is wrong when the catalog does not match the sources.
        """

        adapter = PostgresConnectionAdapter(
            _resolver(connections),
            max_size=pool_size,
            acquire_timeout_seconds=acquire_timeout_seconds,
            pool_factory=pool_factory,
        )
        try:
            inspector = PostgresSourceInspector(adapter)
            runtime = InMemoryCatalogRuntime(
                catalog,
                SourceInspectionRegistry(
                    [InspectionAdapterBinding(
                        source_kind=inspector.source_kind, inspector=inspector, validator=PostgresCatalogValidator())]
                ),
            )
            result = runtime.refresh()
            if runtime.active is None:
                raise CatalogRuntimeError(_activation_failure(result))
            stats = (
                StatisticsService(
                    {SourceKind.POSTGRES: PostgresStatisticsProvider(adapter)}, observations=ObservationStore()
                )
                if statistics
                else None
            )
            extensions = _extensions(semantic)
            planner = FederatedPhysicalPlanner(
                SourcePlanningRegistry([PostgresPlanningAdapter()]),
                policy=planner_policy,
                statistics=stats,
                extensions=extensions,
            )
            engine = QueryExecutionEngine(
                runtime,
                QueryCompilerRegistry([PostgresQueryCompiler()]),
                QueryExecutionAdapterRegistry(
                    [PostgresQueryExecutionAdapter(adapter, default_statement_timeout_seconds=statement_timeout_seconds)]
                ),
                planner=planner,
                statistics=stats,
                extensions=extensions,
                execution_policy=execution_policy,
                join_policy=join_policy,
            )
        except BaseException:
            adapter.close()
            raise
        return cls(runtime, engine, close=adapter.close, semantic=bool(extensions))

    # --- asking -------------------------------------------------------------------------

    def query(self, query: Mapping[str, Any] | str, *, timeout_seconds: float | None = None) -> QueryExecutionResult:
        """Run one logical query.  ``timeout_seconds`` bounds the whole query, every read included."""

        return self._engine.execute(_as_query(query), timeout_seconds=timeout_seconds)

    def explain(self, query: Mapping[str, Any] | str) -> PlanExplanation:
        """The physical plan (sources, pushdown, read order, strategy) without running it."""

        return self._engine.explain(_as_query(query))

    # --- the catalog --------------------------------------------------------------------

    @property
    def catalog(self) -> Catalog:
        return self._runtime.require_active().catalog

    @property
    def semantic_enabled(self) -> bool:
        """Whether a semantic filter was configured, so a query may hold a semantic condition."""

        return self._semantic

    def describe(self) -> dict[str, Any]:
        """What can be queried: each dataset's description and fields with their types."""

        catalog = self.catalog
        return {
            name: {
                "description": dataset.description,
                "fields": {
                    field: {"type": spec.type.value, **({"semantic": True} if spec.semantic_eligible else {})}
                    for field, spec in dataset.fields.items()
                },
            }
            for name, dataset in catalog.datasets.items()
        }

    def status(self) -> dict[str, Any]:
        """The active catalog and each source's validation status."""

        active = self._runtime.require_active()
        return {
            "catalog": {"name": active.catalog.metadata.name, "version": active.catalog.metadata.version},
            "evaluated_at": active.evaluated_at.isoformat(),
            "sources": {name: state.status.value for name, state in active.sources.items()},
        }

    def refresh(self) -> CatalogRefreshResult:
        """Re-read the catalog files and re-inspect the sources; the old catalog stays active if the new one fails."""

        return self._runtime.refresh()

    # --- lifecycle ----------------------------------------------------------------------

    def close(self) -> None:
        if self._close is not None:
            self._close()
            self._close = None

    def __enter__(self) -> "YoDb":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


connect = YoDb.connect


def _resolver(connections: Connections) -> PostgresConnectionReferenceResolver:
    if connections is None:
        return EnvPostgresConnectionResolver()
    if isinstance(connections, Mapping):
        return MappingPostgresConnectionResolver(
            {
                ref: value if isinstance(value, PostgresConnectionSettings) else PostgresConnectionSettings(conninfo=str(value))
                for ref, value in connections.items()
            }
        )
    return connections


def _extensions(semantic: SemanticExtension | SemanticRuntime | None) -> tuple[SemanticExtension, ...]:
    if semantic is None:
        return ()
    if isinstance(semantic, SemanticRuntime):
        return (SemanticExtension(semantic),)
    return (semantic,)


def _as_query(query: Mapping[str, Any] | str) -> Mapping[str, Any]:
    if isinstance(query, str):
        try:
            query = json.loads(query)
        except json.JSONDecodeError as error:
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.QUERY_SHAPE_INVALID,
                    message=f"The query is not valid JSON ({error.msg} at line {error.lineno}, column {error.colno}).",
                    retryable=False,
                )
            ) from error
        except (ValueError, RecursionError) as error:        # e.g. a 100,000-digit number, or absurd nesting
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.QUERY_SHAPE_INVALID,
                    message="The query text is too large or too deeply nested to read.",
                    retryable=False,
                )
            ) from error
    return query


def _activation_failure(result: CatalogRefreshResult) -> ErrorDetail:
    """A safe, specific account of why the catalog did not activate."""

    if result.candidate is None:
        return ErrorDetail(
            code=ErrorCode.CATALOG_LOAD_FAILED,
            message="The catalog YAML files could not be loaded or statically validated "
            "(run `yodb catalog <dir>` for the specific error).",
            retryable=False,
        )
    mismatched: dict[str, list[str]] = {}      # the source answered, and the catalog does not match it
    uninspected: dict[str, list[str]] = {}     # the source could not be inspected at all
    for name, state in result.candidate.sources.items():
        if state.error is not None:
            uninspected[name] = [f"{state.error.code.value}: {state.error.message}"]
        elif state.validation is not None and not state.validation.is_valid:
            mismatched[name] = [f"{f.severity.value}: {f.message}" for f in state.validation.findings]
    parts = []
    if mismatched:
        parts.append("the catalog does not match " + ", ".join(mismatched))
    if uninspected:
        parts.append("could not inspect " + ", ".join(uninspected))
    return ErrorDetail(
        code=ErrorCode.SOURCE_VALIDATION_FAILED,
        message=f"Cannot open the catalog: {'; '.join(parts) or 'a source did not validate'}.",
        retryable=False,
        details={"sources": {**mismatched, **uninspected}},
    )
