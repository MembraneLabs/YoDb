"""Execution handlers: one per physical plan node type, looked up by the executor.

To add an operator, write a handler ``(context, node, run) -> rows`` and add it
to :func:`default_handlers`, or contribute it from an extension
(``ExecutionExtension.handlers``).
The executor itself never changes.
"""

from __future__ import annotations

from ...planning import CoordinatorFilter, CoordinatorSortPage, RecordAssembly, RemoteScan, ResultProject
from . import combine, filter as filter_operator, order_page, project, scan
from .base import (
    ExecutionContext,
    ExecutionExtension,
    ExecutionTrace,
    FederatedExecutionPolicy,
    Handler,
    Run,
    ScanActual,
)


def default_handlers() -> dict[type, Handler]:
    return {
        RemoteScan: scan.execute,
        RecordAssembly: combine.execute,
        CoordinatorFilter: filter_operator.execute,
        CoordinatorSortPage: order_page.execute,
        ResultProject: project.execute,
    }


__all__ = [
    "ExecutionContext",
    "ExecutionExtension",
    "ExecutionTrace",
    "FederatedExecutionPolicy",
    "Handler",
    "Run",
    "ScanActual",
    "default_handlers",
]
