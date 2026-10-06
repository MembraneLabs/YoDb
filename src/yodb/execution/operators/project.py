"""Handler for the PROJECT operator: expose only the requested public fields."""

from __future__ import annotations

from ...planning import ResultProject
from ..contracts import LogicalRow
from .base import ExecutionContext, Run


def execute(ctx: ExecutionContext, node: ResultProject, run: Run) -> tuple[LogicalRow, ...]:
    names = tuple(field.field.name for field in node.projection)
    return tuple({name: row.get(name) for name in names} for row in ctx.execute(node.input, run))
