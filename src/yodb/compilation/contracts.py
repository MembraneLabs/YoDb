"""Backend-specific compiled-query contracts produced below YoDb's logical boundary."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompiledOutputColumn:
    """One selected SQL output column and its logical result-field name."""

    sql_alias: str
    logical_field: str


@dataclass(frozen=True)
class CompiledPostgresQuery:
    """A PostgreSQL statement with catalog-derived identifiers and bound values.

    ``sql`` uses Psycopg's ``%s`` placeholders. User values appear only in
    ``parameters``; SQL identifiers are derived exclusively from the resolved,
    already-validated source binding.
    """

    source_name: str
    connection_ref: str
    sql: str
    parameters: tuple[object, ...]
    output_columns: tuple[CompiledOutputColumn, ...]
