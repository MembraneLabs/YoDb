"""Handler for the SCAN operator: compile one source-local fragment and run it."""

from __future__ import annotations

from ...errors import ErrorCode
from ...planning import RemoteScan
from ..contracts import LogicalRow
from .base import ExecutionContext, Run, ScanActual, fail


def execute(ctx: ExecutionContext, node: RemoteScan, run: Run) -> tuple[LogicalRow, ...]:
    compiled = ctx.compilers.adapter_for(node.source.source_kind).compile_scan(node)
    rows = ctx.executors.adapter_for(compiled.source_kind).execute(compiled, timeout_seconds=run.timeout_seconds)
    if node.maximum_rows is not None and len(rows) > node.maximum_rows:
        fail(
            ErrorCode.QUERY_ROW_LIMIT_EXCEEDED,
            f"Source '{node.source.source_name}' exceeded the V0.1 record-assembly guard of {node.maximum_rows} rows.",
            source_name=node.source.source_name,
        )
    if run.trace is not None and node.limit is None and node.vector_search is None and node.key_filter is None:
        # A complete, unrestricted result: exactly what the planner wants to learn from.
        run.trace.scan_actuals.append(ScanActual(node, len(rows)))
    return rows
