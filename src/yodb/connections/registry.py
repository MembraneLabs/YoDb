"""Selection of a connection adapter from a catalog source kind."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, ContextManager

from ..catalog import SourceKind, SourceSpec
from ..errors import ErrorCode, ErrorDetail, SourceConnectionError
from .contracts import SourceConnectionAdapter


class ConnectionAdapterRegistry:
    """A fixed set of source-kind connection adapters for one YoDb process."""

    def __init__(self, adapters: Iterable[SourceConnectionAdapter[Any]]) -> None:
        self._adapters: dict[SourceKind, SourceConnectionAdapter[Any]] = {}
        for adapter in adapters:
            if adapter.source_kind in self._adapters:
                raise ValueError(f"duplicate connection adapter for '{adapter.source_kind.value}'")
            self._adapters[adapter.source_kind] = adapter

    def adapter_for(self, source_kind: SourceKind) -> SourceConnectionAdapter[Any]:
        try:
            return self._adapters[source_kind]
        except KeyError as error:
            raise SourceConnectionError(
                ErrorDetail(
                    code=ErrorCode.SOURCE_KIND_UNSUPPORTED,
                    message=f"No connection adapter is registered for source kind '{source_kind.value}'.",
                    retryable=False,
                )
            ) from error

    def acquire(
        self,
        source: SourceSpec,
        *,
        timeout_seconds: float | None = None,
    ) -> ContextManager[Any]:
        """Lease a connection using the adapter selected by ``source.kind``."""

        return self.adapter_for(source.kind).acquire(
            source.connection_ref,
            timeout_seconds=timeout_seconds,
        )
