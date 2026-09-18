"""Registry for source-planning capabilities."""

from __future__ import annotations

from collections.abc import Iterable

from ..catalog import SourceKind
from ..errors import ErrorCode, ErrorDetail, QueryError
from ..query.resolution import SingleSourceQueryBinding
from .contracts import SourceCapabilities, SourceCapabilityProvider


class SourceCapabilityRegistry:
    """Fixed capability providers for one YoDb process."""

    def __init__(self, providers: Iterable[tuple[SourceKind, SourceCapabilityProvider]]) -> None:
        self._providers: dict[SourceKind, SourceCapabilityProvider] = {}
        for source_kind, provider in providers:
            if source_kind in self._providers:
                raise ValueError(f"duplicate capability provider for '{source_kind.value}'")
            self._providers[source_kind] = provider

    def capabilities_for(self, source: SingleSourceQueryBinding) -> SourceCapabilities:
        try:
            provider = self._providers[source.source_kind]
        except KeyError as error:
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.SOURCE_CAPABILITY_UNAVAILABLE,
                    message=f"No planning capability provider is registered for '{source.source_kind.value}'.",
                    retryable=False,
                    source_name=source.source_name,
                )
            ) from error
        return provider.capabilities_for(source)

