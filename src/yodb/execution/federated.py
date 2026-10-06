"""Execute V0.1 physical plans without leaking backend concerns upward.

The executor is a thin dispatcher: each physical node type has a handler in
:mod:`yodb.execution.operators`, and ``FederatedPlanExecutor`` only builds the
context they share.  The baseline favors correctness: remote fragments run
through the normal compiler/executor registries, then complete logical
semantics are enforced over normalized rows at the coordinator.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from ..compilation import QueryCompilerRegistry
from ..planning import PhysicalNode
from .contracts import LogicalRow
from .operators import (
    ExecutionContext,
    ExecutionExtension,
    ExecutionTrace,
    FederatedExecutionPolicy,
    Handler,
    Run,
    ScanActual,
    default_handlers,
)
from .registry import QueryExecutionAdapterRegistry

__all__ = ["ExecutionTrace", "FederatedExecutionPolicy", "FederatedPlanExecutor", "ScanActual"]


class FederatedPlanExecutor:
    """Run a physical plan by dispatching each node to its operator's handler."""

    def __init__(
        self,
        compilers: QueryCompilerRegistry,
        executors: QueryExecutionAdapterRegistry,
        *,
        policy: FederatedExecutionPolicy = FederatedExecutionPolicy(),
        extensions: Sequence[ExecutionExtension] = (),
        handlers: Mapping[type, Handler] | None = None,
    ) -> None:
        table = default_handlers()
        for extension in extensions:
            table.update(extension.handlers())
        self._context = ExecutionContext(compilers, executors, policy, {**table, **(handlers or {})})

    def execute(
        self,
        plan: PhysicalNode,
        *,
        timeout_seconds: float | None = None,
        trace: ExecutionTrace | None = None,
    ) -> tuple[LogicalRow, ...]:
        return self._context.execute(plan, Run(timeout_seconds, trace))
