"""Registry for selecting a compiler from a resolved source kind."""

from __future__ import annotations

from collections.abc import Iterable

from ..catalog import SourceKind
from ..errors import ErrorCode, ErrorDetail, QueryError
from .contracts import QueryCompilerAdapter


class QueryCompilerRegistry:
    """A fixed set of backend compiler adapters for one YoDb process."""

    def __init__(self, adapters: Iterable[QueryCompilerAdapter]) -> None:
        self._adapters: dict[SourceKind, QueryCompilerAdapter] = {}
        for adapter in adapters:
            if adapter.source_kind in self._adapters:
                raise ValueError(f"duplicate query compiler for '{adapter.source_kind.value}'")
            self._adapters[adapter.source_kind] = adapter

    def adapter_for(self, source_kind: SourceKind) -> QueryCompilerAdapter:
        """Return the compiler registered for one configured source kind."""

        try:
            return self._adapters[source_kind]
        except KeyError as error:
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.QUERY_COMPILATION_UNSUPPORTED,
                    message=(
                        f"No query compiler is registered for source kind "
                        f"'{source_kind.value}'."
                    ),
                    retryable=False,
                )
            ) from error
