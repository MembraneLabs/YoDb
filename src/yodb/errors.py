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
    QUERY_SHAPE_INVALID = "query_shape_invalid"
    QUERY_FEATURE_NOT_SUPPORTED = "query_feature_not_supported"
    DATASET_NOT_FOUND = "dataset_not_found"
    FIELD_NOT_FOUND = "field_not_found"
    FIELD_NOT_ACCESSIBLE = "field_not_accessible"
    QUERY_VALUE_TYPE_INVALID = "query_value_type_invalid"
    QUERY_OPERATOR_NOT_SUPPORTED = "query_operator_not_supported"
    QUERY_EXPRESSION_INVALID = "query_expression_invalid"
    QUERY_LIMIT_INVALID = "query_limit_invalid"
    CURSOR_QUERY_MISMATCH = "cursor_query_mismatch"
    QUERY_CATALOG_MISMATCH = "query_catalog_mismatch"
    SOURCE_BINDING_UNAVAILABLE = "source_binding_unavailable"
    SOURCE_LOGICAL_ID_UNAVAILABLE = "source_logical_id_unavailable"
    QUERY_COMPILATION_UNSUPPORTED = "query_compilation_unsupported"
    QUERY_SOURCE_SHAPE_UNSUPPORTED = "query_source_shape_unsupported"


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


class QueryError(YoDbError):
    """Raised when a submitted logical query cannot be parsed or validated."""
