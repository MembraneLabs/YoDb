"""Reusable source connection resolution and pooling."""

from .contracts import ConnectionReferenceResolver, SourceConnectionAdapter
from .postgres import (
    EnvPostgresConnectionResolver,
    MappingPostgresConnectionResolver,
    PostgresConnectionAdapter,
    PostgresConnectionReferenceResolver,
    PostgresConnectionSettings,
)
from .registry import ConnectionAdapterRegistry

__all__ = [
    "EnvPostgresConnectionResolver",
    "ConnectionReferenceResolver",
    "ConnectionAdapterRegistry",
    "MappingPostgresConnectionResolver",
    "PostgresConnectionAdapter",
    "PostgresConnectionReferenceResolver",
    "PostgresConnectionSettings",
    "SourceConnectionAdapter",
]
