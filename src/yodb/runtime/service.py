"""Central read-only coordination and activation for an in-memory catalog."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ..catalog import Catalog, CatalogValidationError, load_catalog
from ..errors import CatalogRuntimeError, ErrorCode, ErrorDetail, YoDbError
from ..inspection import InspectionRequest, SourceInspectionRegistry
from .contracts import (
    CatalogEvaluation,
    CatalogRefreshResult,
    RefreshStatus,
    SourceRuntimeState,
    SourceRuntimeStatus,
)


CatalogLoader = Callable[[str | Path], Catalog]
Clock = Callable[[], datetime]


class InMemoryCatalogRuntime:
    """Load, inspect, validate, and atomically retain one active catalog snapshot.

    The runtime is intentionally process-local.  ``refresh`` creates a fresh
    candidate from the three YAML files, delegates source facts to explicitly
    registered adapters, and replaces the active snapshot only when *all*
    configured sources validate.  It never writes to source systems, infers
    mappings, or silently activates a partially valid catalog.

    Connection handling remains inside each source inspector.  For example,
    ``PostgresSourceInspector`` receives a bounded PostgreSQL connection
    adapter at construction time; this coordinator merely selects that already
    registered inspector from a source kind.
    """

    def __init__(
        self,
        catalog_directory: str | Path,
        inspection_registry: SourceInspectionRegistry,
        *,
        catalog_loader: CatalogLoader = load_catalog,
        clock: Clock | None = None,
    ) -> None:
        self._catalog_directory = Path(catalog_directory)
        self._inspection_registry = inspection_registry
        self._catalog_loader = catalog_loader
        self._clock = clock or _utc_now
        self._active: CatalogEvaluation | None = None
        self._last_attempt: CatalogRefreshResult | None = None

    @property
    def active(self) -> CatalogEvaluation | None:
        """The last fully validated catalog, or ``None`` before successful activation."""

        return self._active

    @property
    def last_attempt(self) -> CatalogRefreshResult | None:
        """The latest refresh diagnostics, including a rejected candidate if any."""

        return self._last_attempt

    def require_active(self) -> CatalogEvaluation:
        """Return the active snapshot or raise a safe, typed runtime error.

        Query-serving code should use this method rather than accidentally
        operating with an unvalidated or rejected candidate.
        """

        if self._active is not None:
            return self._active
        raise CatalogRuntimeError(
            ErrorDetail(
                code=ErrorCode.CATALOG_RUNTIME_UNINITIALIZED,
                message="No fully validated catalog snapshot is active.",
                retryable=False,
            )
        )

    def refresh(self) -> CatalogRefreshResult:
        """Load the YAML catalog and attempt a complete in-memory activation."""

        attempted_at = self._clock()
        try:
            catalog = self._catalog_loader(self._catalog_directory)
        except CatalogValidationError:
            return self._record(
                CatalogRefreshResult(
                    status=RefreshStatus.LOAD_FAILED,
                    attempted_at=attempted_at,
                    active=self._active,
                    error=ErrorDetail(
                        code=ErrorCode.CATALOG_LOAD_FAILED,
                        message="The catalog YAML files could not be loaded or statically validated.",
                        retryable=False,
                    ),
                )
            )

        candidate = self._evaluate(catalog, attempted_at)
        if candidate.is_activatable:
            self._active = candidate
            return self._record(
                CatalogRefreshResult(
                    status=RefreshStatus.ACTIVATED,
                    attempted_at=attempted_at,
                    candidate=candidate,
                    active=candidate,
                )
            )

        return self._record(
            CatalogRefreshResult(
                status=RefreshStatus.REJECTED,
                attempted_at=attempted_at,
                candidate=candidate,
                active=self._active,
            )
        )

    def _evaluate(self, catalog: Catalog, evaluated_at: datetime) -> CatalogEvaluation:
        states: dict[str, SourceRuntimeState] = {}
        for source_name, source in catalog.sources.items():
            try:
                adapter = self._inspection_registry.binding_for(source.kind)
                inspection = adapter.inspector.inspect(
                    InspectionRequest(source_name=source_name, source=source)
                )
                if (
                    inspection.source_name != source_name
                    or inspection.source_kind is not source.kind
                ):
                    raise _invalid_adapter_output(source_name, "inspection")
                report = adapter.validator.validate(catalog, inspection)
                if report.source_name != source_name:
                    raise _invalid_adapter_output(source_name, "validation report")
                status = SourceRuntimeStatus.VALID if report.is_valid else SourceRuntimeStatus.INVALID
                states[source_name] = SourceRuntimeState(
                    source_name=source_name,
                    status=status,
                    inspection=inspection,
                    validation=report,
                )
            except YoDbError as error:
                states[source_name] = SourceRuntimeState(
                    source_name=source_name,
                    status=SourceRuntimeStatus.INSPECTION_FAILED,
                    error=_with_source(error.detail, source_name),
                )
            except Exception:
                # Do not leak a driver exception, connection string, or query
                # into the catalog status API.  Provider adapters should raise
                # YoDbError, but this boundary remains safe if one does not.
                states[source_name] = SourceRuntimeState(
                    source_name=source_name,
                    status=SourceRuntimeStatus.INSPECTION_FAILED,
                    error=ErrorDetail(
                        code=ErrorCode.SOURCE_INSPECTION_FAILED,
                        message="Source inspection failed unexpectedly.",
                        retryable=False,
                        source_name=source_name,
                    ),
                )
        return CatalogEvaluation(catalog=catalog, evaluated_at=evaluated_at, sources=states)

    def _record(self, result: CatalogRefreshResult) -> CatalogRefreshResult:
        self._last_attempt = result
        return result


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _with_source(detail: ErrorDetail, source_name: str) -> ErrorDetail:
    if detail.source_name == source_name:
        return detail
    return detail.model_copy(update={"source_name": source_name})


def _invalid_adapter_output(source_name: str, output_name: str) -> YoDbError:
    return YoDbError(
        ErrorDetail(
            code=ErrorCode.SOURCE_INSPECTION_FAILED,
            message=(
                f"The registered source adapter returned a mismatched {output_name}."
            ),
            retryable=False,
            source_name=source_name,
        )
    )
