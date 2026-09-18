"""Registry for source-specific federated cost estimators."""

from __future__ import annotations

from collections.abc import Iterable

from ..catalog import SourceKind
from ..errors import ErrorCode, ErrorDetail, QueryError
from .contracts import SourceCostEstimator


class CostEstimatorRegistry:
    """Fixed cost-estimator adapters for one YoDb process."""

    def __init__(self, adapters: Iterable[SourceCostEstimator]) -> None:
        self._adapters: dict[SourceKind, SourceCostEstimator] = {}
        for adapter in adapters:
            if adapter.source_kind in self._adapters:
                raise ValueError(f"duplicate cost estimator for '{adapter.source_kind.value}'")
            self._adapters[adapter.source_kind] = adapter

    def adapter_for(self, source_kind: SourceKind) -> SourceCostEstimator:
        try:
            return self._adapters[source_kind]
        except KeyError as error:
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.SOURCE_CAPABILITY_UNAVAILABLE,
                    message=f"No cost estimator is registered for source kind '{source_kind.value}'.",
                    retryable=False,
                )
            ) from error

