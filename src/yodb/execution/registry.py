"""Registry for selecting a query executor from a compiled command's backend."""

from __future__ import annotations

from collections.abc import Iterable

from ..catalog import SourceKind
from ..errors import ErrorCode, ErrorDetail, QueryExecutionError
from .contracts import QueryExecutionAdapter


class QueryExecutionAdapterRegistry:
    """A fixed set of backend execution adapters for one YoDb process."""

    def __init__(self, adapters: Iterable[QueryExecutionAdapter]) -> None:
        self._adapters: dict[SourceKind, QueryExecutionAdapter] = {}
        for adapter in adapters:
            if adapter.source_kind in self._adapters:
                raise ValueError(f"duplicate query executor for '{adapter.source_kind.value}'")
            self._adapters[adapter.source_kind] = adapter

    def adapter_for(self, source_kind: SourceKind) -> QueryExecutionAdapter:
        """Return the executor registered for one compiled command's backend."""

        try:
            return self._adapters[source_kind]
        except KeyError as error:
            raise QueryExecutionError(
                ErrorDetail(
                    code=ErrorCode.QUERY_EXECUTION_UNSUPPORTED,
                    message=(
                        f"No query execution adapter is registered for source kind "
                        f"'{source_kind.value}'."
                    ),
                    retryable=False,
                )
            ) from error
