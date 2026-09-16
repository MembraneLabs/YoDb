"""Structured, transport-neutral errors shared by YoDb modules.

Adapters raise :class:`YoDbError`; callers can serialize its ``detail`` without
exposing driver exceptions, credentials, connection strings, or raw queries.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(str, Enum):
    """V0.1 error codes needed before query execution exists."""

    SOURCE_KIND_UNSUPPORTED = "source_kind_unsupported"
    CONNECTION_REFERENCE_NOT_FOUND = "connection_reference_not_found"
    CONNECTION_POOL_TIMEOUT = "connection_pool_timeout"
    CONNECTION_POOL_CLOSED = "connection_pool_closed"
    SOURCE_UNAVAILABLE = "source_unavailable"
    SOURCE_AUTHENTICATION_FAILED = "source_authentication_failed"
    SOURCE_PERMISSION_DENIED = "source_permission_denied"
    SOURCE_INSPECTION_FAILED = "source_inspection_failed"
    SOURCE_CAPABILITY_UNAVAILABLE = "source_capability_unavailable"
    SOURCE_VALIDATION_FAILED = "source_validation_failed"
    CATALOG_LOAD_FAILED = "catalog_load_failed"
    CATALOG_RUNTIME_UNINITIALIZED = "catalog_runtime_uninitialized"


class ErrorDetail(BaseModel):
    """The stable, safe-to-return error payload for a YoDb operation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    message: str = Field(min_length=1)
    retryable: bool
    source_name: str | None = None
    location: str | None = None
    details: dict[str, Any] = {}


class YoDbError(Exception):
    """A typed exception carrying one safe structured error detail."""

    def __init__(self, detail: ErrorDetail) -> None:
        self.detail = detail
        super().__init__(detail.message)

    @property
    def code(self) -> ErrorCode:
        return self.detail.code

    @property
    def retryable(self) -> bool:
        return self.detail.retryable


class SourceInspectionError(YoDbError):
    """Raised when an adapter cannot produce a source inspection snapshot."""


class SourceConnectionError(YoDbError):
    """Raised when a source connection cannot be resolved or leased."""


class CatalogRuntimeError(YoDbError):
    """Raised when an in-memory catalog runtime has no usable active snapshot."""
