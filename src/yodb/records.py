"""Canonical in-memory records, edges, validation, and lifecycle behavior."""

from __future__ import annotations

from base64 import b64decode
from binascii import Error as Base64Error
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
from types import MappingProxyType
from typing import Any, Mapping
from uuid import UUID

from .errors import EdgeValidationError, RecordValidationError, ValidationViolation, YoDbError
from .ids import is_yodb_id, new_id
from .specs import Cardinality, CatalogSpec, DatasetSpec, FieldSpec, FieldType, RelationshipSpec


_RESERVED_INPUT_FIELDS = frozenset({"id"})


@dataclass(frozen=True, slots=True)
class CanonicalRecord:
    id: str
    dataset: str
    schema_version: int
    fields: Mapping[str, Any]
    extra: Mapping[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "dataset": self.dataset,
            "schema_version": self.schema_version,
            "fields": dict(self.fields),
            "extra": dict(self.extra),
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }


@dataclass(frozen=True, slots=True)
class CanonicalEdge:
    id: str
    relationship: str
    source_id: str
    target_id: str
    schema_version: int
    fields: Mapping[str, Any]
    version: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "relationship": self.relationship,
            "source_id": self.source_id,
            "target_id": self.target_id,
            "schema_version": self.schema_version,
            "fields": dict(self.fields),
            "version": self.version,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }


class PayloadValidator:
    """Normalizes typed declared fields and optional JSON-compatible extras."""

    def validate_create(self, spec: DatasetSpec | RelationshipSpec, values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._validate(spec, values, partial=False)

    def validate_update(self, spec: DatasetSpec | RelationshipSpec, values: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return self._validate(spec, values, partial=True)

    def _validate(
        self, spec: DatasetSpec | RelationshipSpec, values: Mapping[str, Any], *, partial: bool
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if not isinstance(values, Mapping):
            self._raise(spec, [ValidationViolation("fields", "invalid_type", "Expected an object.")])

        declared = spec.fields
        normalized: dict[str, Any] = {}
        extra: dict[str, Any] = {}
        violations: list[ValidationViolation] = []
        allow_unknown = isinstance(spec, DatasetSpec) and spec.allow_unknown_fields

        for name, value in values.items():
            if not isinstance(name, str):
                violations.append(
                    ValidationViolation("fields", "invalid_field_name", "Field names must be strings.")
                )
                continue
            if name in _RESERVED_INPUT_FIELDS:
                violations.append(
                    ValidationViolation(f"fields.{name}", "reserved_field", "This field is system-owned.")
                )
                continue
            field_spec = declared.get(name)
            if field_spec is None:
                if not allow_unknown:
                    violations.append(
                        ValidationViolation(f"fields.{name}", "unknown_field", "This field is not declared.")
                    )
                else:
                    try:
                        extra[name] = _normalize_json(value)
                    except ValueError as error:
                        violations.append(ValidationViolation(f"fields.{name}", "invalid_json", str(error)))
                continue
            try:
                normalized[name] = _normalize_field(field_spec, value)
            except ValueError as error:
                violations.append(ValidationViolation(f"fields.{name}", "invalid_value", str(error)))

        if not partial:
            for name, field_spec in declared.items():
                if name == "id" or name in values:
                    continue
                if field_spec.required:
                    violations.append(
                        ValidationViolation(f"fields.{name}", "required_field_missing", "This field is required.")
                    )
                elif field_spec.has_default:
                    try:
                        normalized[name] = _normalize_field(field_spec, deepcopy(field_spec.default))
                    except ValueError as error:
                        violations.append(ValidationViolation(f"fields.{name}", "invalid_default", str(error)))

        if violations:
            self._raise(spec, violations)
        return normalized, extra

    @staticmethod
    def _raise(spec: DatasetSpec | RelationshipSpec, violations: list[ValidationViolation]) -> None:
        if isinstance(spec, DatasetSpec):
            raise RecordValidationError(spec.qualified_name, violations)
        raise EdgeValidationError(spec.name, violations)


def _normalize_field(spec: FieldSpec, value: Any) -> Any:
    if value is None:
        if not spec.nullable:
            raise ValueError("This field is not nullable.")
        return None
    if spec.repeated:
        if not isinstance(value, list):
            raise ValueError("A repeated field requires a list.")
        scalar_spec = replace(spec, repeated=False)
        return tuple(_normalize_field(scalar_spec, item) for item in value)
    return _normalize_scalar(spec.type, value)


def _normalize_scalar(field_type: FieldType, value: Any) -> Any:
    if field_type in {FieldType.STRING, FieldType.TEXT}:
        if not isinstance(value, str):
            raise ValueError("Expected a string.")
        return value
    if field_type is FieldType.INT:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("Expected an integer.")
        return value
    if field_type is FieldType.FLOAT:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError("Expected a number.")
        return float(value)
    if field_type is FieldType.BOOL:
        if not isinstance(value, bool):
            raise ValueError("Expected a boolean.")
        return value
    if field_type is FieldType.TIMESTAMP:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("Expected an ISO-8601 timestamp.") from error
        else:
            raise ValueError("Expected an ISO-8601 timestamp.")
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("Timestamp must include a timezone.")
        return parsed.astimezone(UTC)
    if field_type is FieldType.UUID:
        if isinstance(value, UUID):
            return value
        if not isinstance(value, str):
            raise ValueError("Expected a canonical UUID string.")
        try:
            parsed = UUID(value)
        except ValueError as error:
            raise ValueError("Expected a canonical UUID string.") from error
        if str(parsed) != value:
            raise ValueError("Expected a canonical lowercase UUID string.")
        return parsed
    if field_type is FieldType.ID:
        if not isinstance(value, str) or not is_yodb_id(value):
            raise ValueError("Expected a YoDb logical ID.")
        return value
    if field_type is FieldType.JSON:
        return _normalize_json(value)
    if field_type is FieldType.BYTES:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            try:
                return b64decode(value, validate=True)
            except (ValueError, Base64Error) as error:
                raise ValueError("Expected bytes or a base64 string.") from error
        raise ValueError("Expected bytes or a base64 string.")
    raise ValueError(f"Unsupported field type: {field_type!s}.")


def _normalize_json(value: Any) -> Any:
    try:
        serialized = json.dumps(value, allow_nan=False, separators=(",", ":"))
        return json.loads(serialized)
    except (TypeError, ValueError) as error:
        raise ValueError("Expected a JSON-compatible value.") from error


def freeze_mapping(values: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(deepcopy(dict(values)))
