"""Handler for the ORDER and LIMIT operators when YoDb orders and pages."""

from __future__ import annotations

from functools import cmp_to_key

from ...errors import ErrorCode
from ...planning import CoordinatorSortPage
from ..contracts import LogicalRow
from .base import ExecutionContext, Run, fail
from .rows import compare_rows


def execute(ctx: ExecutionContext, node: CoordinatorSortPage, run: Run) -> tuple[LogicalRow, ...]:
    rows = list(ctx.execute(node.input, run))
    if len(rows) > ctx.policy.maximum_coordinator_rows:
        fail(
            ErrorCode.QUERY_COORDINATOR_LIMIT_EXCEEDED,
            f"Coordinator processing exceeded {ctx.policy.maximum_coordinator_rows} rows.",
        )
    if node.order_by:
        rows.sort(key=cmp_to_key(lambda left, right: compare_rows(left, right, node.order_by)))
    if node.first is not None:
        rows = rows[: node.first]
    return tuple(rows)
