from __future__ import annotations

from datetime import UTC, datetime
import unittest

from yodb.catalog import Catalog, CatalogMetadata, DatasetSpec, FieldSpec, LogicalType, Visibility
from yodb.errors import ErrorCode, QueryError
from yodb.query import QueryValidationPolicy, bind_query, parse_query
from yodb.runtime import CatalogEvaluation


class QueryModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.active = CatalogEvaluation(
            catalog=Catalog(
                metadata=CatalogMetadata(name="acme_data", version=1),
                datasets={
                    "customer": DatasetSpec(
                        description="A commercial account.",
                        fields={
                            "id": FieldSpec(type=LogicalType.ID, description="Stable logical identity."),
                            "name": FieldSpec(type=LogicalType.STRING, description="Customer name."),
                            "status": FieldSpec(type=LogicalType.STRING, description="Lifecycle status."),
                            "created_at": FieldSpec(
                                type=LogicalType.TIMESTAMP, description="Creation instant."
                            ),
                            "active": FieldSpec(type=LogicalType.BOOL, description="Whether active."),
                            "account_key": FieldSpec(
                                type=LogicalType.UUID,
                                description="Internal source key.",
                                visibility=Visibility.INTERNAL,
                            ),
                            "metadata": FieldSpec(type=LogicalType.JSON, description="Unstructured metadata."),
                        },
                    )
                },
                sources={},
                resolution={},
                relationships={},
            ),
            evaluated_at=datetime(2026, 9, 15, tzinfo=UTC),
            sources={},
        )

    def test_binds_public_fields_normalizes_values_and_adds_id_ordering(self) -> None:
        request = parse_query(
            {
                "from": {"dataset": "customer"},
                "select": ["name", "status"],
                "where": {
                    "all": [
                        {"field": "status", "op": "eq", "value": "active"},
                        {"field": "created_at", "op": "gte", "value": "2026-01-01T00:00:00Z"},
                    ]
                },
                "order_by": [{"field": "name", "direction": "asc"}],
                "page": {"first": 25},
            }
        )

        bound = bind_query(request, self.active)

        self.assertEqual([field.name for field in bound.select], ["id", "name", "status"])
        self.assertEqual([term.field.name for term in bound.order_by], ["name", "id"])
        self.assertEqual(bound.page.first, 25)
        self.assertEqual(len(bound.query_fingerprint), 64)
        self.assertEqual(len(bound.catalog_fingerprint), 64)

    def test_equivalent_boolean_order_and_page_size_share_a_query_fingerprint(self) -> None:
        first = parse_query(
            {
                "from": {"dataset": "customer"},
                "where": {
                    "all": [
                        {"field": "status", "op": "eq", "value": "active"},
                        {"field": "active", "op": "eq", "value": True},
                    ]
                },
                "page": {"first": 10},
            }
        )
        second = parse_query(
            {
                "page": {"first": 50, "after": "opaque_cursor_from_a_previous_page"},
                "where": {
                    "all": [
                        {"value": True, "op": "eq", "field": "active"},
                        {"value": "active", "field": "status", "op": "eq"},
                    ]
                },
                "from": {"dataset": "customer"},
            }
        )

        bound_first = bind_query(first, self.active)
        bound_second = bind_query(second, self.active)

        self.assertEqual(bound_first.query_fingerprint, bound_second.query_fingerprint)
        self.assertEqual(bound_first.catalog_fingerprint, bound_second.catalog_fingerprint)

    def test_rejects_unknown_and_internal_fields(self) -> None:
        with self.assertRaises(QueryError) as unknown:
            bind_query(parse_query({"from": {"dataset": "customer"}, "select": ["missing"]}), self.active)
        self.assertEqual(unknown.exception.code, ErrorCode.FIELD_NOT_FOUND)
        self.assertEqual(unknown.exception.detail.location, "select[0]")

        with self.assertRaises(QueryError) as internal:
            bind_query(
                parse_query(
                    {
                        "from": {"dataset": "customer"},
                        "where": {"field": "account_key", "op": "eq", "value": "a" * 36},
                    }
                ),
                self.active,
            )
        self.assertEqual(internal.exception.code, ErrorCode.FIELD_NOT_ACCESSIBLE)

    def test_rejects_incompatible_values_and_operators(self) -> None:
        with self.assertRaises(QueryError) as bad_boolean:
            bind_query(
                parse_query(
                    {"from": {"dataset": "customer"}, "where": {"field": "active", "op": "eq", "value": "true"}}
                ),
                self.active,
            )
        self.assertEqual(bad_boolean.exception.code, ErrorCode.QUERY_VALUE_TYPE_INVALID)

        with self.assertRaises(QueryError) as bad_operator:
            bind_query(
                parse_query(
                    {"from": {"dataset": "customer"}, "where": {"field": "active", "op": "contains", "value": "true"}}
                ),
                self.active,
            )
        self.assertEqual(bad_operator.exception.code, ErrorCode.QUERY_OPERATOR_NOT_SUPPORTED)

        with self.assertRaises(QueryError) as unordered_json:
            bind_query(
                parse_query(
                    {"from": {"dataset": "customer"}, "order_by": [{"field": "metadata", "direction": "asc"}]}
                ),
                self.active,
            )
        self.assertEqual(unordered_json.exception.code, ErrorCode.QUERY_OPERATOR_NOT_SUPPORTED)

    def test_parser_rejects_unknown_shape_and_reserved_features(self) -> None:
        with self.assertRaises(QueryError) as unknown:
            parse_query({"from": {"dataset": "customer"}, "sql": "SELECT * FROM accounts"})
        self.assertEqual(unknown.exception.code, ErrorCode.QUERY_SHAPE_INVALID)

        with self.assertRaises(QueryError) as semantic:
            parse_query(
                {"from": {"dataset": "customer"}, "semantic": {"field": "name", "query": "acme"}}
            )
        self.assertEqual(semantic.exception.code, ErrorCode.QUERY_FEATURE_NOT_SUPPORTED)

    def test_uses_the_configured_bound_for_in_lists(self) -> None:
        request = parse_query(
            {
                "from": {"dataset": "customer"},
                "where": {"field": "status", "op": "in", "value": ["active", "paused"]},
            }
        )
        with self.assertRaises(QueryError) as caught:
            bind_query(request, self.active, policy=QueryValidationPolicy(maximum_in_values=1))
        self.assertEqual(caught.exception.code, ErrorCode.QUERY_LIMIT_INVALID)
