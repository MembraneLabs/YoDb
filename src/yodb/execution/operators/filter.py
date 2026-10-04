"""Handler for the FILTER operator when YoDb (not a source) applies the filter."""

from __future__ import annotations

from ...planning import CoordinatorFilter
from ..contracts import LogicalRow
from .base import ExecutionContext, Run
from .rows import matches


def execute(ctx: ExecutionContext, node: CoordinatorFilter, run: Run) -> tuple[LogicalRow, ...]:
    return tuple(row for row in ctx.execute(node.input, run) if matches(node.expression, row))
