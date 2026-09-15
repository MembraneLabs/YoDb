"""Process-local catalog loading, inspection coordination, and activation state."""

from .contracts import (
    CatalogEvaluation,
    CatalogRefreshResult,
    RefreshStatus,
    SourceRuntimeState,
    SourceRuntimeStatus,
)
from .service import InMemoryCatalogRuntime

__all__ = [
    "CatalogEvaluation",
    "CatalogRefreshResult",
    "InMemoryCatalogRuntime",
    "RefreshStatus",
    "SourceRuntimeState",
    "SourceRuntimeStatus",
]
