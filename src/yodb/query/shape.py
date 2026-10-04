"""Small shape-validation helpers shared by the query parser and term extensions."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import ErrorCode, ErrorDetail, QueryError


def fail(code: ErrorCode, message: str, location: str | None = None) -> None:
    raise QueryError(ErrorDetail(code=code, message=message, retryable=False, location=location))


def mapping(raw: Any, location: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        fail(ErrorCode.QUERY_SHAPE_INVALID, "Expected an object.", location)
    if not all(isinstance(key, str) for key in raw):
        fail(ErrorCode.QUERY_SHAPE_INVALID, "Object keys must be strings.", location)
    return raw


def list_of(raw: Any, location: str) -> list[Any]:
    if not isinstance(raw, list):
        fail(ErrorCode.QUERY_SHAPE_INVALID, "Expected an array.", location)
    return raw


def non_empty_string(raw: Any, location: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        fail(ErrorCode.QUERY_SHAPE_INVALID, "Expected a non-empty string.", location)
    return raw


def reject_unknown(value: Mapping[str, Any], allowed: frozenset[str], location: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        fail(ErrorCode.QUERY_SHAPE_INVALID, f"Unknown field(s): {', '.join(unknown)}.", location)
