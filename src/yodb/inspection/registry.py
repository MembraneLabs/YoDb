"""Registry for source inspection and validation adapters.

The registry selects an explicit adapter pair by the source kind declared in
``sources.yaml``.  It never attempts provider detection or mapping inference.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from ..catalog import SourceKind
from ..errors import ErrorCode, ErrorDetail, SourceInspectionError
from .contracts import SourceCatalogValidator, SourceInspector


@dataclass(frozen=True, slots=True)
class InspectionAdapterBinding:
    """The inspector and validator explicitly registered for one source kind."""

    source_kind: SourceKind
    inspector: SourceInspector
    validator: SourceCatalogValidator


class SourceInspectionRegistry:
    """An immutable-by-convention lookup of inspection adapters for one process."""

    def __init__(self, bindings: Iterable[InspectionAdapterBinding]) -> None:
        self._bindings: dict[SourceKind, InspectionAdapterBinding] = {}
        for binding in bindings:
            if binding.source_kind in self._bindings:
                raise ValueError(
                    f"duplicate inspection adapter for '{binding.source_kind.value}'"
                )
            self._bindings[binding.source_kind] = binding

    def binding_for(self, source_kind: SourceKind) -> InspectionAdapterBinding:
        """Return the registered adapter pair for ``source_kind``.

        A missing adapter is a source-specific activation failure, rather than
        a reason to skip source validation or infer a substitute backend.
        """

        try:
            return self._bindings[source_kind]
        except KeyError as error:
            raise SourceInspectionError(
                ErrorDetail(
                    code=ErrorCode.SOURCE_KIND_UNSUPPORTED,
                    message=(
                        "No inspection adapter is registered for source kind "
                        f"'{source_kind.value}'."
                    ),
                    retryable=False,
                )
            ) from error
