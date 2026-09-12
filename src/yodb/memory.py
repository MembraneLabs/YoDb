"""Thread-safe in-memory canonical store used by the first domain layer."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from threading import RLock
from typing import Any, Mapping

from .errors import YoDbError
from .ids import new_id
from .records import CanonicalEdge, CanonicalRecord, PayloadValidator, freeze_mapping
from .specs import Cardinality, CatalogSpec, DatasetSpec, RelationshipSpec


class InMemoryCanonicalStore:
    """Canonical lifecycle semantics without a persistence backend.

    This is deliberately a single-process reference implementation. PostgreSQL
    will later provide the same behavior durably and transactionally.
    """

    def __init__(self, catalog: CatalogSpec) -> None:
        self._catalog = catalog
        self._datasets = {item.qualified_name: item for item in catalog.datasets}
        self._relationships = {f"{item.namespace}.{item.name}": item for item in catalog.relationships}
        self._records: dict[str, CanonicalRecord] = {}
        self._edges: dict[str, CanonicalEdge] = {}
        self._validator = PayloadValidator()
        self._lock = RLock()

    def create_record(self, dataset: str, fields: Mapping[str, Any]) -> CanonicalRecord:
        with self._lock:
            spec = self._dataset(dataset)
            normalized, extra = self._validator.validate_create(spec, fields)
            now = _utcnow()
            record = CanonicalRecord(
                id=new_id(),
                dataset=spec.qualified_name,
                schema_version=spec.version,
                fields=freeze_mapping(normalized),
                extra=freeze_mapping(extra),
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._records[record.id] = record
            return record

    def get_record(self, record_id: str) -> CanonicalRecord:
        with self._lock:
            return self._record(record_id)

    def update_record(
        self, record_id: str, fields: Mapping[str, Any], *, expected_version: int
    ) -> CanonicalRecord:
        with self._lock:
            record = self._record(record_id)
            self._check_version(record.version, expected_version)
            spec = self._dataset(record.dataset)
            normalized, extra = self._validator.validate_update(spec, fields)
            updated = replace(
                record,
                fields=freeze_mapping({**record.fields, **normalized}),
                extra=freeze_mapping({**record.extra, **extra}),
                version=record.version + 1,
                updated_at=_utcnow(),
            )
            self._records[record_id] = updated
            return updated

    def delete_record(self, record_id: str, *, expected_version: int) -> CanonicalRecord:
        with self._lock:
            record = self._record(record_id)
            self._check_version(record.version, expected_version)
            now = _utcnow()
            deleted = replace(record, version=record.version + 1, updated_at=now, deleted_at=now)
            self._records[record_id] = deleted
            for edge_id, edge in tuple(self._edges.items()):
                if edge.deleted_at is None and record_id in {edge.source_id, edge.target_id}:
                    self._edges[edge_id] = replace(
                        edge, version=edge.version + 1, updated_at=now, deleted_at=now
                    )
            return deleted

    def create_edge(
        self,
        relationship: str,
        source_id: str,
        target_id: str,
        fields: Mapping[str, Any] | None = None,
    ) -> CanonicalEdge:
        with self._lock:
            spec = self._relationship(relationship)
            source = self._record(source_id)
            target = self._record(target_id)
            self._validate_edge_endpoints(spec, source, target)
            normalized, extra = self._validator.validate_create(spec, fields or {})
            if extra:
                raise AssertionError("Relationship validation cannot produce extras.")
            self._check_edge_constraints(spec, source_id, target_id)
            now = _utcnow()
            edge = CanonicalEdge(
                id=new_id(),
                relationship=f"{spec.namespace}.{spec.name}",
                source_id=source_id,
                target_id=target_id,
                schema_version=spec.version,
                fields=freeze_mapping(normalized),
                version=1,
                created_at=now,
                updated_at=now,
            )
            self._edges[edge.id] = edge
            return edge

    def get_edge(self, edge_id: str) -> CanonicalEdge:
        with self._lock:
            return self._edge(edge_id)

    def update_edge(
        self, edge_id: str, fields: Mapping[str, Any], *, expected_version: int
    ) -> CanonicalEdge:
        with self._lock:
            edge = self._edge(edge_id)
            self._check_version(edge.version, expected_version)
            spec = self._relationship(edge.relationship)
            normalized, extra = self._validator.validate_update(spec, fields)
            if extra:
                raise AssertionError("Relationship validation cannot produce extras.")
            updated = replace(
                edge,
                fields=freeze_mapping({**edge.fields, **normalized}),
                version=edge.version + 1,
                updated_at=_utcnow(),
            )
            self._edges[edge_id] = updated
            return updated

    def delete_edge(self, edge_id: str, *, expected_version: int) -> CanonicalEdge:
        with self._lock:
            edge = self._edge(edge_id)
            self._check_version(edge.version, expected_version)
            now = _utcnow()
            deleted = replace(edge, version=edge.version + 1, updated_at=now, deleted_at=now)
            self._edges[edge_id] = deleted
            return deleted

    def _dataset(self, name: str) -> DatasetSpec:
        qualified = name if "." in name else f"default.{name}"
        try:
            return self._datasets[qualified]
        except KeyError as error:
            raise YoDbError("dataset_not_found", f"Dataset {name!r} does not exist.") from error

    def _relationship(self, name: str) -> RelationshipSpec:
        qualified = name if "." in name else f"default.{name}"
        try:
            return self._relationships[qualified]
        except KeyError as error:
            raise YoDbError("relationship_not_found", f"Relationship {name!r} does not exist.") from error

    def _record(self, record_id: str) -> CanonicalRecord:
        record = self._records.get(record_id)
        if record is None or record.deleted_at is not None:
            raise YoDbError("record_not_found", f"Record {record_id!r} does not exist.")
        return record

    def _edge(self, edge_id: str) -> CanonicalEdge:
        edge = self._edges.get(edge_id)
        if edge is None or edge.deleted_at is not None:
            raise YoDbError("edge_not_found", f"Edge {edge_id!r} does not exist.")
        return edge

    @staticmethod
    def _check_version(actual: int, expected: int) -> None:
        if actual != expected:
            raise YoDbError(
                "version_conflict",
                "The record version does not match the expected version.",
                details={"expected_version": expected, "actual_version": actual},
            )

    def _validate_edge_endpoints(
        self, spec: RelationshipSpec, source: CanonicalRecord, target: CanonicalRecord
    ) -> None:
        expected_source = f"{spec.namespace}.{spec.from_dataset}"
        expected_target = f"{spec.namespace}.{spec.to_dataset}"
        if source.dataset != expected_source or target.dataset != expected_target:
            raise YoDbError(
                "record_validation_failed",
                "Relationship endpoints do not match the declared relationship datasets.",
                details={
                    "relationship": spec.name,
                    "expected_source_dataset": expected_source,
                    "expected_target_dataset": expected_target,
                },
            )

    def _check_edge_constraints(self, spec: RelationshipSpec, source_id: str, target_id: str) -> None:
        active = [
            edge
            for edge in self._edges.values()
            if edge.deleted_at is None and edge.relationship == f"{spec.namespace}.{spec.name}"
        ]
        if any(edge.source_id == source_id and edge.target_id == target_id for edge in active):
            raise YoDbError("duplicate_edge", "An active edge with these endpoints already exists.")
        source_used = any(edge.source_id == source_id for edge in active)
        target_used = any(edge.target_id == target_id for edge in active)
        if spec.cardinality is Cardinality.ONE_TO_ONE and (source_used or target_used):
            self._cardinality_error(spec)
        if spec.cardinality is Cardinality.ONE_TO_MANY and target_used:
            self._cardinality_error(spec)
        if spec.cardinality is Cardinality.MANY_TO_ONE and source_used:
            self._cardinality_error(spec)

    @staticmethod
    def _cardinality_error(spec: RelationshipSpec) -> None:
        raise YoDbError(
            "cardinality_violation",
            f"Relationship {spec.name!r} would violate {spec.cardinality.value} cardinality.",
            details={"relationship": spec.name, "cardinality": spec.cardinality.value},
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)
