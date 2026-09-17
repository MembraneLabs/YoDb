"""Backend-neutral contracts for executing compiled source-local queries."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from ..catalog import SourceKind
from ..compilation import CompiledQuery


LogicalRow = Mapping[str, object]


@dataclass(frozen=True)
class QueryExecutionResult:
    """Backend-neutral rows produced for one logical query.

    ``rows`` contain logical field names only. Native cursor, node, document,
    and driver objects never leave an execution adapter.
    """

    rows: tuple[LogicalRow, ...]
    query_fingerprint: str
    catalog_fingerprint: str

    @classmethod
    def from_rows(
        cls,
        rows: tuple[Mapping[str, object], ...],
        *,
        query_fingerprint: str,
        catalog_fingerprint: str,
    ) -> "QueryExecutionResult":
        """Freeze adapter-returned row mappings before exposing them."""

        return cls(
            rows=tuple(MappingProxyType(dict(row)) for row in rows),
            query_fingerprint=query_fingerprint,
            catalog_fingerprint=catalog_fingerprint,
        )


@runtime_checkable
class QueryExecutionAdapter(Protocol):
    """Run compiled commands for exactly one source backend."""

    @property
    def source_kind(self) -> SourceKind:
        """The backend kind handled by this adapter."""

    def execute(
        self,
        query: CompiledQuery,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[LogicalRow, ...]:
        """Execute the command and return rows keyed by logical field name."""


@runtime_checkable
class ActiveCatalogProvider(Protocol):
    """The active, fully validated catalog snapshot used by query serving."""

    def require_active(self) -> Any:
        """Return the catalog evaluation used to bind and resolve a query."""
