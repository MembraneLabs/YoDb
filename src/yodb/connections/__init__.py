"""Reusable source connection resolution and pooling."""

from .contracts import ConnectionReferenceResolver, SourceConnectionAdapter
from .postgres import (
    MappingPostgresConnectionResolver,
    PostgresConnectionAdapter,
    PostgresConnectionReferenceResolver,
    PostgresConnectionSettings,
)
from .registry import ConnectionAdapterRegistry

__all__ = [
    "ConnectionReferenceResolver",
    "ConnectionAdapterRegistry",
    "MappingPostgresConnectionResolver",
    "PostgresConnectionAdapter",
    "PostgresConnectionReferenceResolver",
    "PostgresConnectionSettings",
    "SourceConnectionAdapter",
]
