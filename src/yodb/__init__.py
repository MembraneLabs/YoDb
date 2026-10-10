"""YoDb: a read-only, typed, explainable query layer over several databases.

``yodb.connect(catalog_dir)`` is the way in; see ``yodb.client``.  The names below are the
stable public surface (docs: reference/compatibility).  Everything else lives in its own
subpackage (``yodb.planning``, ``yodb.inspection``, ...) and may change between versions.
"""

from .catalog import Catalog, CatalogValidationError, load_catalog
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

__version__ = "0.1.1"

__all__ = [
    "YoDb",
    "connect",
    "Catalog",
    "CatalogRefreshResult",
    "CatalogRuntimeError",
    "CatalogValidationError",
    "ErrorCode",
    "ErrorDetail",
    "PlanExplanation",
    "PlanExplanationNode",
    "QueryError",
    "QueryExecutionError",
    "QueryExecutionResult",
    "SourceConnectionError",
    "SourceInspectionError",
    "YoDbError",
    "load_catalog",
]

# Loaded on first use, so importing ``yodb`` (or a core subpackage) never pulls in the engine
# or the semantic filter.
_LAZY = {
    "YoDb": "client",
    "connect": "client",
    "QueryExecutionResult": "execution",
    "PlanExplanation": "planning",
    "PlanExplanationNode": "planning",
    "CatalogRefreshResult": "runtime",
}


def __getattr__(name: str):
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'yodb' has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f".{module}", __name__), name)
