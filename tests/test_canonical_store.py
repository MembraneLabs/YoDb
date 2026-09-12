import unittest
from datetime import UTC, datetime

from yodb import (
    Cardinality,
    CatalogSpec,
    DatasetSpec,
    FieldSpec,
    FieldType,
    InMemoryCanonicalStore,
    RelationshipSpec,
    YoDbError,
)


class CanonicalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        documents = DatasetSpec(
            name="documents",
            allow_unknown_fields=True,
            fields={
                "id": FieldSpec(FieldType.ID),
                "title": FieldSpec(FieldType.STRING, required=True),
                "status": FieldSpec(FieldType.STRING, default="draft"),
                "created_at": FieldSpec(FieldType.TIMESTAMP, nullable=True),
                "published_at": FieldSpec(FieldType.TIMESTAMP, nullable=True),
                "tags": FieldSpec(FieldType.STRING, repeated=True),
            },
        )
        users = DatasetSpec(
            name="users",
            fields={"id": FieldSpec(FieldType.ID), "name": FieldSpec(FieldType.STRING, required=True)},
        )
        authored_by = RelationshipSpec(
            name="authored_by",
            from_dataset="documents",
            to_dataset="users",
            cardinality=Cardinality.MANY_TO_ONE,
            fields={"role": FieldSpec(FieldType.STRING, required=True)},
        )
        self.store = InMemoryCanonicalStore(CatalogSpec((documents, users), (authored_by,)))

    def test_create_normalizes_defaults_types_and_unknown_fields(self) -> None:
        record = self.store.create_record(
            "documents",
            {
                "title": "Roadmap",
                "created_at": "2026-09-12T09:00:00+02:00",
                "published_at": "2026-09-12T10:00:00+02:00",
                "tags": ["plan", "architecture"],
                "source_payload": {"external": True},
            },
        )

        self.assertTrue(record.id.startswith("ydb_"))
        self.assertEqual(record.version, 1)
        self.assertEqual(record.fields["status"], "draft")
        self.assertEqual(record.fields["created_at"], datetime(2026, 9, 12, 7, 0, tzinfo=UTC))
        self.assertEqual(record.fields["published_at"], datetime(2026, 9, 12, 8, 0, tzinfo=UTC))
        self.assertEqual(record.fields["tags"], ("plan", "architecture"))
        self.assertEqual(record.extra, {"source_payload": {"external": True}})

    def test_create_rejects_invalid_and_reserved_fields(self) -> None:
        with self.assertRaises(YoDbError) as raised:
            self.store.create_record("documents", {"id": "ydb_client", "title": 7})

        self.assertEqual(raised.exception.code, "record_validation_failed")
        codes = {item["code"] for item in raised.exception.details["violations"]}
        self.assertEqual(codes, {"reserved_field", "invalid_value"})

    def test_required_fields_and_unknown_fields_are_enforced(self) -> None:
        with self.assertRaises(YoDbError) as raised:
            self.store.create_record("users", {"nickname": "Ada"})

        self.assertEqual(raised.exception.code, "record_validation_failed")
        codes = {item["code"] for item in raised.exception.details["violations"]}
        self.assertEqual(codes, {"required_field_missing", "unknown_field"})

    def test_update_requires_expected_version_and_preserves_omitted_fields(self) -> None:
        record = self.store.create_record("documents", {"title": "First"})
        updated = self.store.update_record(record.id, {"title": "Second"}, expected_version=1)

        self.assertEqual(updated.version, 2)
        self.assertEqual(updated.fields["status"], "draft")
        with self.assertRaises(YoDbError) as raised:
            self.store.update_record(record.id, {"title": "Third"}, expected_version=1)
        self.assertEqual(raised.exception.code, "version_conflict")
        self.assertEqual(raised.exception.details["actual_version"], 2)

    def test_edges_enforce_cardinality_and_endpoint_types(self) -> None:
        document = self.store.create_record("documents", {"title": "Plan"})
        other_document = self.store.create_record("documents", {"title": "Other"})
        user = self.store.create_record("users", {"name": "Ada"})
        edge = self.store.create_edge("authored_by", document.id, user.id, {"role": "primary"})

        self.assertEqual(edge.version, 1)
        self.assertEqual(edge.schema_version, 1)
        with self.assertRaises(YoDbError) as duplicate:
            self.store.create_edge("authored_by", document.id, user.id, {"role": "primary"})
        self.assertEqual(duplicate.exception.code, "duplicate_edge")
        with self.assertRaises(YoDbError) as cardinality:
            self.store.create_edge("authored_by", document.id, self.store.create_record("users", {"name": "Lin"}).id, {"role": "secondary"})
        self.assertEqual(cardinality.exception.code, "cardinality_violation")
        with self.assertRaises(YoDbError) as endpoint:
            self.store.create_edge("authored_by", document.id, other_document.id, {"role": "primary"})
        self.assertEqual(endpoint.exception.code, "record_validation_failed")

    def test_record_deletion_tombstones_incident_edges(self) -> None:
        document = self.store.create_record("documents", {"title": "Plan"})
        user = self.store.create_record("users", {"name": "Ada"})
        edge = self.store.create_edge("authored_by", document.id, user.id, {"role": "primary"})

        deleted = self.store.delete_record(document.id, expected_version=1)

        self.assertIsNotNone(deleted.deleted_at)
        self.assertEqual(deleted.version, 2)
        with self.assertRaises(YoDbError) as record_missing:
            self.store.get_record(document.id)
        self.assertEqual(record_missing.exception.code, "record_not_found")
        with self.assertRaises(YoDbError) as edge_missing:
            self.store.get_edge(edge.id)
        self.assertEqual(edge_missing.exception.code, "edge_not_found")


if __name__ == "__main__":
    unittest.main()
