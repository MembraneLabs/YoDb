from __future__ import annotations

from datetime import UTC, datetime
import unittest

from yodb.catalog import DatasetSpec, FieldSpec, LogicalType, SourceKind
from yodb.compilation import PostgresQueryCompiler
from yodb.errors import ErrorCode, QueryError
from yodb.query import (
    BoundAllExpression,
    BoundDataset,
    BoundField,
    BoundNotExpression,
    BoundOrderTerm,
    BoundPredicate,
    BoundQuery,
    ComparisonOperator,
    FieldUse,
    PageRequest,
    QueryConstraints,
    QuerySourceShape,
    ResolvedField,
    SingleSourceQueryBinding,
    SortDirection,
    SourceResolvedQuery,
)


class PostgresQueryCompilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.compiler = PostgresQueryCompiler()

    def test_compiles_projection_filter_order_and_limit_with_bound_values(self) -> None:
        query = _single_source_query(
            where=BoundPredicate(
                _field("status", LogicalType.STRING),
                ComparisonOperator.EQ,
                "active'; DROP TABLE accounts; --",
                True,
            )
        )

        compiled = self.compiler.compile(query)

        self.assertEqual(compiled.source_name, "crm_postgres")
        self.assertEqual(compiled.connection_ref, "secret://crm-readonly")
        self.assertEqual(
            compiled.sql,
            "\n".join(
                (
                    'SELECT "account_uuid" AS "id", "company_name" AS "name", "account_status" AS "status"',
                    'FROM "public"."accounts"',
                    'WHERE "account_status" = %s',
                    'ORDER BY "company_name" ASC, "account_uuid" ASC',
                    'LIMIT %s',
                )
            ),
        )
        self.assertEqual(compiled.parameters, ("active'; DROP TABLE accounts; --", 25))
        self.assertNotIn("DROP TABLE", compiled.sql)
        self.assertEqual(
            [(column.sql_alias, column.logical_field) for column in compiled.output_columns],
            [("id", "id"), ("name", "name"), ("status", "status")],
        )

    def test_compiles_boolean_range_and_membership_expression(self) -> None:
        created_at = _field("created_at", LogicalType.TIMESTAMP)
        query = _single_source_query(
            where=BoundAllExpression(
                (
                    BoundPredicate(
                        _field("status", LogicalType.STRING),
                        ComparisonOperator.IN,
                        ("active", "pending"),
                        True,
                    ),
                    BoundNotExpression(
                        BoundPredicate(
                            created_at,
                            ComparisonOperator.LT,
                            datetime(2026, 1, 1, tzinfo=UTC),
                            True,
                        )
                    ),
                )
            )
        )

        compiled = self.compiler.compile(query)

        self.assertIn('WHERE ("account_status" IN (%s, %s) AND (NOT "created_at" < %s))', compiled.sql)
        self.assertEqual(
            compiled.parameters,
            ("active", "pending", datetime(2026, 1, 1, tzinfo=UTC), 25),
        )

    def test_applies_a_result_affecting_maximum_results_constraint(self) -> None:
        compiled = self.compiler.compile(_single_source_query(maximum_results=10))

        self.assertEqual(compiled.parameters, (10,))

    def test_rejects_multi_source_cursor_and_not_yet_compiled_text_operators(self) -> None:
        multi = _single_source_query(shape=QuerySourceShape.MULTI_SOURCE)
        with self.assertRaises(QueryError) as multi_error:
            self.compiler.compile(multi)
        self.assertEqual(multi_error.exception.code, ErrorCode.QUERY_SOURCE_SHAPE_UNSUPPORTED)

        cursor = _single_source_query(after="signed_cursor")
        with self.assertRaises(QueryError) as cursor_error:
            self.compiler.compile(cursor)
        self.assertEqual(cursor_error.exception.code, ErrorCode.QUERY_COMPILATION_UNSUPPORTED)
        self.assertEqual(cursor_error.exception.detail.location, "page.after")

        text = _single_source_query(
            where=BoundPredicate(
                _field("name", LogicalType.STRING), ComparisonOperator.CONTAINS, "Acme", True
            )
        )
        with self.assertRaises(QueryError) as text_error:
            self.compiler.compile(text)
        self.assertEqual(text_error.exception.code, ErrorCode.QUERY_COMPILATION_UNSUPPORTED)


def _single_source_query(
    *,
    where: object | None = None,
    after: str | None = None,
    maximum_results: int | None = None,
    shape: QuerySourceShape = QuerySourceShape.SINGLE_SOURCE,
) -> SourceResolvedQuery:
    dataset = DatasetSpec(
        description="A customer account.",
        fields={
            "id": FieldSpec(type=LogicalType.ID, description="Stable identity."),
            "name": FieldSpec(type=LogicalType.STRING, description="Customer name."),
            "status": FieldSpec(type=LogicalType.STRING, description="Customer lifecycle state."),
            "created_at": FieldSpec(type=LogicalType.TIMESTAMP, description="Creation time."),
        },
    )
    root = BoundDataset(name="customer", scope="customer", spec=dataset)
    selected = (_field("id", LogicalType.ID), _field("name", LogicalType.STRING), _field("status", LogicalType.STRING))
    query = BoundQuery(
        root=root,
        select=selected,
        where=where,
        order_by=(
            BoundOrderTerm(_field("name", LogicalType.STRING), SortDirection.ASC),
            BoundOrderTerm(_field("id", LogicalType.ID), SortDirection.ASC),
        ),
        page=PageRequest(first=25, after=after),
        constraints=QueryConstraints(maximum_results=maximum_results),
        query_fingerprint="query",
        catalog_fingerprint="catalog",
    )
    fields = (
        _resolved(_field("id", LogicalType.ID), "account_uuid", {FieldUse.IDENTITY, FieldUse.PROJECTION, FieldUse.ORDER}),
        _resolved(_field("name", LogicalType.STRING), "company_name", {FieldUse.PROJECTION, FieldUse.ORDER}),
        _resolved(_field("status", LogicalType.STRING), "account_status", {FieldUse.PROJECTION, FieldUse.FILTER}),
        _resolved(_field("created_at", LogicalType.TIMESTAMP), "created_at", {FieldUse.FILTER}),
    )
    source = SingleSourceQueryBinding(
        source_name="crm_postgres",
        source_kind=SourceKind.POSTGRES,
        connection_ref="secret://crm-readonly",
        resource="public.accounts",
        logical_id=fields[0],
        fields=fields,
    )
    return SourceResolvedQuery(
        query=query,
        shape=shape,
        identity_source=source,
        sources=(source,),
        logical_id_links=(),
    )


def _field(name: str, type_: LogicalType) -> BoundField:
    return BoundField(
        dataset_name="customer",
        scope="customer",
        name=name,
        spec=FieldSpec(type=type_, description=f"{name} field."),
    )


def _resolved(field: BoundField, physical_name: str, uses: set[FieldUse]) -> ResolvedField:
    return ResolvedField(
        field=field,
        source_name="crm_postgres",
        source_kind=SourceKind.POSTGRES,
        connection_ref="secret://crm-readonly",
        resource="public.accounts",
        physical_name=physical_name,
        uses=frozenset(uses),
    )
