"""YoDb: a read-only, typed, explainable query layer over several databases.

``yodb.connect(catalog_dir)`` is the short path; see ``yodb.client``.
"""

from .catalog import Catalog, CatalogValidationError, load_catalog
from .connections import (
    EnvPostgresConnectionResolver,
    ConnectionReferenceResolver,
    ConnectionAdapterRegistry,
    MappingPostgresConnectionResolver,
    PostgresConnectionAdapter,
    PostgresConnectionReferenceResolver,
    PostgresConnectionSettings,
    SourceConnectionAdapter,
)
from .compilation import (
    CompiledOutputColumn,
    CompiledPostgresQuery,
    CompiledQuery,
    PostgresQueryCompiler,
    QueryCompilerAdapter,
    QueryCompilerRegistry,
)
from .errors import (
    CatalogRuntimeError,
    ErrorCode,
    ErrorDetail,
    QueryError,
    QueryExecutionError,
    SourceConnectionError,
    SourceInspectionError,
    YoDbError,
)
from .execution import (
    PostgresQueryExecutionAdapter,
    QueryExecutionAdapter,
    QueryExecutionAdapterRegistry,
    QueryExecutionEngine,
    QueryExecutionResult,
)
from .query import (
    BoundQuery,
    FieldUse,
    LogicalIdLink,
    QueryRequest,
    QuerySourceShape,
    QueryValidationPolicy,
    ResolvedField,
    SingleSourceQueryBinding,
    SourceResolvedQuery,
    bind_query,
    parse_query,
    resolve_query_sources,
    validate_query,
)
from .inspection import (
    FindingSeverity,
    InspectionCapability,
    InspectionRequest,
    GraphRelationshipType,
    PhysicalCheckConstraint,
    PhysicalField,
    PhysicalForeignKey,
    PhysicalIndex,
    PhysicalKey,
    PhysicalResource,
    PhysicalUniqueConstraint,
    PostgresCatalogValidator,
    PostgresSourceInspector,
    ResourceKind,
    InspectionAdapterBinding,
    SourceCatalogValidator,
    SourceInspection,
    SourceInspectionRegistry,
    SourceInspector,
    SourceValidationReport,
    ValidationFinding,
)
from .runtime import (
    CatalogEvaluation,
    CatalogRefreshResult,
    InMemoryCatalogRuntime,
    RefreshStatus,
    SourceRuntimeState,
    SourceRuntimeStatus,
)

__all__ = [
    "YoDb",
    "connect",
    "Catalog",
    "CatalogEvaluation",
    "CatalogRefreshResult",
    "CatalogRuntimeError",
    "CatalogValidationError",
    "ConnectionReferenceResolver",
    "CompiledOutputColumn",
    "CompiledPostgresQuery",
    "CompiledQuery",
    "ConnectionAdapterRegistry",
    "EnvPostgresConnectionResolver",
    "ErrorCode",
    "ErrorDetail",
    "QueryError",
    "QueryExecutionError",
    "FindingSeverity",
    "GraphRelationshipType",
    "InspectionCapability",
    "InspectionAdapterBinding",
    "InspectionRequest",
    "MappingPostgresConnectionResolver",
    "InMemoryCatalogRuntime",
    "PhysicalField",
    "PhysicalCheckConstraint",
    "PhysicalForeignKey",
    "PhysicalIndex",
    "PhysicalKey",
    "PhysicalResource",
    "PhysicalUniqueConstraint",
    "PostgresCatalogValidator",
    "PostgresQueryCompiler",
    "PostgresQueryExecutionAdapter",
    "PostgresConnectionAdapter",
    "PostgresConnectionReferenceResolver",
    "PostgresConnectionSettings",
    "PostgresSourceInspector",
    "ResourceKind",
    "RefreshStatus",
    "SourceCatalogValidator",
    "SourceConnectionAdapter",
    "SourceConnectionError",
    "SourceInspectionError",
    "SourceInspection",
    "SourceInspectionRegistry",
    "SourceInspector",
    "SourceValidationReport",
    "SourceRuntimeState",
    "SourceRuntimeStatus",
    "ValidationFinding",
    "YoDbError",
    "QueryCompilerAdapter",
    "QueryCompilerRegistry",
    "QueryExecutionAdapter",
    "QueryExecutionAdapterRegistry",
    "QueryExecutionEngine",
    "QueryExecutionResult",
    "BoundQuery",
    "FieldUse",
    "LogicalIdLink",
    "QueryRequest",
    "QuerySourceShape",
    "QueryValidationPolicy",
    "ResolvedField",
    "SingleSourceQueryBinding",
    "SourceResolvedQuery",
    "bind_query",
    "load_catalog",
    "parse_query",
    "resolve_query_sources",
    "validate_query",
]


def __getattr__(name: str):
    """``yodb.connect`` / ``yodb.YoDb`` load the front door on first use, so importing a core
    subpackage (``yodb.query``, ``yodb.planning``, ``yodb.execution``) never pulls in the semantic filter."""

    if name in ("YoDb", "connect"):
        from . import client

        return getattr(client, name)
    raise AttributeError(f"module 'yodb' has no attribute {name!r}")
