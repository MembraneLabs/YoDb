"""Central query execution engine and backend execution adapters."""

from .contracts import (
    ActiveCatalogProvider,
    LogicalRow,
    QueryExecutionAdapter,
    QueryExecutionResult,
)
from .engine import QueryExecutionEngine
from .in_memory import assemble
from .postgres import PostgresQueryExecutionAdapter
from .registry import QueryExecutionAdapterRegistry

__all__ = [
    "ActiveCatalogProvider",
    "LogicalRow",
    "PostgresQueryExecutionAdapter",
    "QueryExecutionAdapter",
    "QueryExecutionAdapterRegistry",
    "QueryExecutionEngine",
    "QueryExecutionResult",
    "assemble",
]
