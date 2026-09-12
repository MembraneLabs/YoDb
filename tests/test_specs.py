import unittest

from yodb import (
    Cardinality,
    CatalogSpec,
    DatasetSpec,
    FieldSpec,
    FieldType,
    IndexKind,
    IndexSpec,
    RelationshipSpec,
    SpecValidationError,
)


class SpecTests(unittest.TestCase):
    def setUp(self) -> None:
        self.documents = DatasetSpec(
            name="documents",
            fields={
                "id": FieldSpec(FieldType.ID),
                "title": FieldSpec(FieldType.STRING, searchable=True, filterable=True),
                "content": FieldSpec(FieldType.TEXT, searchable=True),
                "created_at": FieldSpec(FieldType.TIMESTAMP, sortable=True),
            },
            indexes=(
                IndexSpec(
                    name="content_embedding",
                    kind=IndexKind.VECTOR,
                    source_field="content",
                    embedding_model="text-embedding-3-large",
                    dimensions=3072,
                    distance="cosine",
                ),
            ),
        )

    def test_catalog_with_relationship_and_vector_index(self) -> None:
        users = DatasetSpec(
            name="users",
            fields={"id": FieldSpec(FieldType.ID), "name": FieldSpec(FieldType.STRING)},
        )
        authored_by = RelationshipSpec(
            name="authored_by",
            from_dataset="documents",
            to_dataset="users",
            cardinality=Cardinality.MANY_TO_ONE,
            fields={"role": FieldSpec(FieldType.STRING)},
        )

        catalog = CatalogSpec((self.documents, users), (authored_by,))

        self.assertEqual(catalog.datasets[0].indexes[0].source_field, "content")
        self.assertEqual(catalog.to_dict()["relationships"][0]["name"], "authored_by")
        self.assertEqual(catalog.to_dict()["relationships"][0]["version"], 1)

    def test_dataset_requires_logical_id(self) -> None:
        with self.assertRaisesRegex(SpecValidationError, "'id' field"):
            DatasetSpec(name="missing_id", fields={"name": FieldSpec(FieldType.STRING)})

    def test_field_requirement_and_default_semantics(self) -> None:
        required = FieldSpec(FieldType.STRING, required=True)
        defaulted = FieldSpec(FieldType.STRING, default="draft")
        nullable = FieldSpec(FieldType.TEXT, nullable=True, default=None)

        self.assertFalse(required.has_default)
        self.assertEqual(defaulted.to_dict()["default"], "draft")
        self.assertIsNone(nullable.to_dict()["default"])
        with self.assertRaisesRegex(SpecValidationError, "both required"):
            FieldSpec(FieldType.STRING, required=True, default="draft")
        with self.assertRaisesRegex(SpecValidationError, "null default"):
            FieldSpec(FieldType.STRING, default=None)

    def test_unknown_fields_are_disabled_by_default(self) -> None:
        self.assertFalse(self.documents.allow_unknown_fields)
        permissive = DatasetSpec(
            name="ingest_events",
            allow_unknown_fields=True,
            fields={"id": FieldSpec(FieldType.ID)},
        )
        self.assertTrue(permissive.allow_unknown_fields)

    def test_vector_index_requires_text_source(self) -> None:
        with self.assertRaisesRegex(SpecValidationError, "string or text"):
            DatasetSpec(
                name="bad_vectors",
                fields={"id": FieldSpec(FieldType.ID), "count": FieldSpec(FieldType.INT)},
                indexes=(
                    IndexSpec(
                        name="count_embedding",
                        kind=IndexKind.VECTOR,
                        source_field="count",
                        embedding_model="test-model",
                        dimensions=3,
                        distance="cosine",
                    ),
                ),
            )

    def test_relationship_references_existing_datasets(self) -> None:
        missing = RelationshipSpec(
            name="missing_target",
            from_dataset="documents",
            to_dataset="unknown",
            cardinality=Cardinality.MANY_TO_ONE,
        )
        with self.assertRaisesRegex(SpecValidationError, "existing datasets"):
            CatalogSpec((self.documents,), (missing,))


if __name__ == "__main__":
    unittest.main()
