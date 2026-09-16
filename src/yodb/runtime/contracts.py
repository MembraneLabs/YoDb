"""In-memory catalog-runtime state and coordination contracts.

These models represent a candidate catalog activation.  They retain the
user-authored :class:`~yodb.catalog.Catalog`, factual source-inspection
snapshots, and validation reports in one process-local object.  They do not
persist source data or change mappings.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..catalog import Catalog
from ..errors import ErrorDetail
from ..inspection.contracts import SourceInspection, SourceValidationReport


class RuntimeModel(BaseModel):
    """Strict immutable value model used by the in-memory runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRuntimeStatus(str, Enum):
    """The outcome of inspecting and validating a configured source."""

    VALID = "valid"
    INVALID = "invalid"
    INSPECTION_FAILED = "inspection_failed"


class RefreshStatus(str, Enum):
    """Whether the most recent refresh activated, rejected, or could not load a catalog."""

    ACTIVATED = "activated"
    REJECTED = "rejected"
    LOAD_FAILED = "load_failed"


class SourceRuntimeState(RuntimeModel):
    """All factual state held for one source in a candidate catalog version."""

    source_name: str = Field(min_length=1)
    status: SourceRuntimeStatus
    inspection: SourceInspection | None = None
    validation: SourceValidationReport | None = None
    error: ErrorDetail | None = None

    @model_validator(mode="after")
    def requires_consistent_outcome(self) -> "SourceRuntimeState":
        if self.status is SourceRuntimeStatus.INSPECTION_FAILED:
            if self.error is None or self.inspection is not None or self.validation is not None:
                raise ValueError("an inspection failure requires only a structured error")
        elif self.status is SourceRuntimeStatus.VALID:
            if self.inspection is None or self.validation is None or not self.validation.is_valid:
                raise ValueError("a valid source requires a valid inspection and validation report")
            if self.error is not None:
                raise ValueError("a valid source cannot carry an error")
        elif self.inspection is None or self.validation is None or self.validation.is_valid:
            raise ValueError("an invalid source requires an invalid validation report")
        return self

    @property
    def is_healthy(self) -> bool:
        """Whether this source permits activation of its catalog version."""

        return self.status is SourceRuntimeStatus.VALID


class CatalogEvaluation(RuntimeModel):
    """One complete candidate catalog and its in-memory inspection metadata."""

    catalog: Catalog
    evaluated_at: datetime
    sources: dict[str, SourceRuntimeState]

    @model_validator(mode="after")
    def contains_every_configured_source(self) -> "CatalogEvaluation":
        expected = set(self.catalog.sources)
        actual = set(self.sources)
        if actual != expected:
            raise ValueError(
                "runtime source state must match configured sources "
                f"(missing={sorted(expected - actual)}, extra={sorted(actual - expected)})"
            )
        for source_name, state in self.sources.items():
            if state.source_name != source_name:
                raise ValueError("runtime source state key must match source_name")
        return self

    @property
    def is_activatable(self) -> bool:
        """Whether every configured source inspected and validated successfully."""

        return all(state.is_healthy for state in self.sources.values())


class CatalogRefreshResult(RuntimeModel):
    """One refresh attempt and the active version retained by the runtime.

    A rejected candidate never replaces ``active``.  This makes activation an
    all-or-nothing in-memory swap while retaining diagnostics for the failed
    attempt.
    """

    status: RefreshStatus
    attempted_at: datetime
    candidate: CatalogEvaluation | None = None
    active: CatalogEvaluation | None = None
    error: ErrorDetail | None = None

    @model_validator(mode="after")
    def requires_consistent_result(self) -> "CatalogRefreshResult":
        if self.status is RefreshStatus.ACTIVATED:
            if self.candidate is None or self.active != self.candidate or self.error is not None:
                raise ValueError("an activated refresh requires its candidate as the active snapshot")
        elif self.status is RefreshStatus.REJECTED:
            if self.candidate is None or self.candidate.is_activatable or self.error is not None:
                raise ValueError("a rejected refresh requires a non-activatable candidate")
        elif self.candidate is not None or self.error is None:
            raise ValueError("a load failure requires only a structured error")
        return self
