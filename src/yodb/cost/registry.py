from __future__ import annotations

from collections.abc import Iterable

from ..catalog import SourceKind
from ..errors import ErrorCode, ErrorDetail, QueryError
from .contracts import SourceCostEstimator


class CostEstimatorRegistry:
    def __init__(self, adapters: Iterable[SourceCostEstimator]) -> None:
        values = tuple(adapters)
        self._adapters = {adapter.source_kind: adapter for adapter in values}
        if len(self._adapters) != len(values):
            raise ValueError("duplicate source cost estimator")

    def adapter_for(self, kind: SourceKind) -> SourceCostEstimator:
        try:
            return self._adapters[kind]
        except KeyError as error:
            raise QueryError(ErrorDetail(code=ErrorCode.QUERY_COMPILATION_UNSUPPORTED, message=f"No cost estimator is registered for source kind '{kind.value}'.", retryable=False)) from error
