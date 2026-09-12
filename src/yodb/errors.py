"""Public, backend-independent YoDb errors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ValidationViolation:
    """One field-level reason a record or edge payload was rejected."""

    path: str
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "code": self.code, "message": self.message}


class YoDbError(Exception):
    """A stable error contract suitable for any future transport layer."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = dict(details or {})

    def to_dict(self, *, request_id: str | None = None) -> dict[str, Any]:
        error: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }
        if request_id is not None:
            error["request_id"] = request_id
        return {"error": error}


class RecordValidationError(YoDbError):
    def __init__(self, dataset: str, violations: list[ValidationViolation]) -> None:
        super().__init__(
            "record_validation_failed",
            f"Record is invalid for dataset {dataset!r}.",
            details={"dataset": dataset, "violations": [item.to_dict() for item in violations]},
        )
        self.violations = tuple(violations)


class EdgeValidationError(YoDbError):
    def __init__(self, relationship: str, violations: list[ValidationViolation]) -> None:
        super().__init__(
            "record_validation_failed",
            f"Edge is invalid for relationship {relationship!r}.",
            details={
                "relationship": relationship,
                "violations": [item.to_dict() for item in violations],
            },
        )
        self.violations = tuple(violations)
