"""Registry for source-specific semantic planning adapters."""

from __future__ import annotations

from collections.abc import Iterable

from ..catalog import SourceKind
from ..errors import ErrorCode, ErrorDetail, QueryError
from .contracts import SourcePlanningAdapter


class SourcePlanningRegistry:
    """Fixed planning adapters for one YoDb process."""

    def __init__(self, adapters: Iterable[SourcePlanningAdapter]) -> None:
        self._adapters: dict[SourceKind, SourcePlanningAdapter] = {}
        for adapter in adapters:
            if adapter.source_kind in self._adapters:
                raise ValueError(f"duplicate planning adapter for '{adapter.source_kind.value}'")
            self._adapters[adapter.source_kind] = adapter

    def adapter_for(self, source_kind: SourceKind) -> SourcePlanningAdapter:
        try:
            return self._adapters[source_kind]
        except KeyError as error:
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.SOURCE_CAPABILITY_UNAVAILABLE,
                    message=f"No planning adapter is registered for '{source_kind.value}'.",
                    retryable=False,
                )
            ) from error

