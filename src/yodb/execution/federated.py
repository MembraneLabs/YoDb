"""Execute V0.1 physical plans without leaking backend concerns upward.

The baseline deliberately favors correctness: remote fragments are executed
through the normal compiler/executor registries, then complete logical
semantics are enforced over normalized rows at the coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from functools import cmp_to_key
from typing import Any

from ..compilation import QueryCompilerRegistry
from ..errors import ErrorCode, ErrorDetail, QueryExecutionError
from ..planning import (
    CoordinatorFilter,
    CoordinatorSortPage,
    PhysicalPlan,
    RecordAssembly,
    RemoteScan,
    ResultProject,
)
from ..query.models import (
    BoundAllExpression,
    BoundAnyExpression,
    BoundFilterExpression,
    BoundNotExpression,
    BoundPredicate,
    ComparisonOperator,
    SortDirection,
)
from .contracts import LogicalRow
from .registry import QueryExecutionAdapterRegistry


@dataclass(frozen=True)
class FederatedExecutionPolicy:
    """Coordinator safeguards for bounded V0.1 record assembly."""

    maximum_coordinator_rows: int = 50_000

    def __post_init__(self) -> None:
        if self.maximum_coordinator_rows < 1:
            raise ValueError("maximum_coordinator_rows must be positive")


class FederatedPlanExecutor:
    """Interpret the small physical-plan vocabulary used by V0.1."""

    def __init__(
        self,
        compilers: QueryCompilerRegistry,
        executors: QueryExecutionAdapterRegistry,
        *,
        policy: FederatedExecutionPolicy = FederatedExecutionPolicy(),
    ) -> None:
        self._compilers = compilers
        self._executors = executors
        self._policy = policy

    def execute(
        self,
        plan: PhysicalPlan,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[LogicalRow, ...]:
        return self._execute(plan, timeout_seconds=timeout_seconds)

    def _execute(self, plan: PhysicalPlan, *, timeout_seconds: float | None) -> tuple[LogicalRow, ...]:
        if isinstance(plan, RemoteScan):
            compiler = self._compilers.adapter_for(plan.source.source_kind)
            compiled = compiler.compile_scan(plan)
            rows = self._executors.adapter_for(compiled.source_kind).execute(
                compiled, timeout_seconds=timeout_seconds
            )
            if plan.maximum_rows is not None and len(rows) > plan.maximum_rows:
                _fail(
                    ErrorCode.QUERY_ROW_LIMIT_EXCEEDED,
                    (
                        f"Source '{plan.source.source_name}' exceeded the V0.1 record-assembly "
                        f"guard of {plan.maximum_rows} rows."
                    ),
                    source_name=plan.source.source_name,
                )
            return rows
        if isinstance(plan, RecordAssembly):
            return self._assemble(plan, timeout_seconds=timeout_seconds)
        if isinstance(plan, CoordinatorFilter):
            return tuple(
                row
                for row in self._execute(plan.input, timeout_seconds=timeout_seconds)
                if _matches(plan.expression, row)
            )
        if isinstance(plan, CoordinatorSortPage):
            rows = list(self._execute(plan.input, timeout_seconds=timeout_seconds))
            if len(rows) > self._policy.maximum_coordinator_rows:
                _fail(
                    ErrorCode.QUERY_COORDINATOR_LIMIT_EXCEEDED,
                    f"Coordinator processing exceeded {self._policy.maximum_coordinator_rows} rows.",
                )
            if plan.order_by:
                rows.sort(key=cmp_to_key(lambda left, right: _compare_rows(left, right, plan.order_by)))
            if plan.first is not None:
                rows = rows[: plan.first]
            return tuple(rows)
        if isinstance(plan, ResultProject):
            rows = self._execute(plan.input, timeout_seconds=timeout_seconds)
            names = tuple(field.field.name for field in plan.projection)
            return tuple({name: row.get(name) for name in names} for row in rows)
        raise AssertionError(f"Unknown physical plan: {plan!r}")

    def _assemble(
        self,
        plan: RecordAssembly,
        *,
        timeout_seconds: float | None,
    ) -> tuple[LogicalRow, ...]:
        """Run the scans in a key-transfer order, then left-enrich the anchor.

        Contributors with a pushed filter are *required matches*: an anchor row
        survives only if the contributor also returned it.  They run first and
        each narrows the ID set (intersection) that restricts the next scan and
        finally the anchor.  Optional contributors run last, restricted to the
        anchor IDs.  A key set larger than ``plan.maximum_transfer_keys`` is not
        transferred (that scan runs unrestricted, still under its row guard),
        and an empty key set ends the query without further source reads.
        """

        required = [c for c in plan.contributors if c.source.source_name in plan.required_contributor_matches]
        optional = [c for c in plan.contributors if c.source.source_name not in plan.required_contributor_matches]
        keys: set[object] | None = None
        required_rows: list[tuple[RemoteScan, tuple[LogicalRow, ...]]] = []
        required_ids: list[set[object]] = []
        for contributor in required:
            rows = self._scan(contributor, keys, plan.maximum_transfer_keys, timeout_seconds)
            ids = {row["id"] for row in rows}
            required_rows.append((contributor, rows))
            required_ids.append(ids)
            keys = ids if keys is None else keys & ids
            if not keys:
                return ()

        anchor_rows = self._scan(plan.anchor, keys, plan.maximum_transfer_keys, timeout_seconds)
        self._ensure_unique_ids(anchor_rows, plan.anchor.source.source_name)
        records: dict[object, dict[str, object]] = {row["id"]: dict(row) for row in anchor_rows}
        if not records:
            return ()

        optional_rows = [
            (contributor, self._scan(contributor, set(records), plan.maximum_transfer_keys, timeout_seconds))
            for contributor in optional
        ]
        for contributor, rows in (*required_rows, *optional_rows):
            for row in rows:
                target = records.get(row["id"])
                if target is not None:
                    # The identity is a join key, never a competing field value.
                    target.update((name, value) for name, value in row.items() if name != "id")
        return tuple(
            record
            for logical_id, record in records.items()
            if all(logical_id in ids for ids in required_ids)
        )

    def _scan(
        self,
        scan: RemoteScan,
        keys: set[object] | None,
        maximum_keys: int | None,
        timeout_seconds: float | None,
    ) -> tuple[LogicalRow, ...]:
        """Execute one scan, restricted to ``keys`` when that set is small enough."""

        if keys is not None and maximum_keys is not None and len(keys) <= maximum_keys:
            scan = replace(scan, key_filter=tuple(sorted(keys, key=str)))
        rows = self._execute(scan, timeout_seconds=timeout_seconds)
        self._ensure_unique_ids(rows, scan.source.source_name)
        return rows

    @staticmethod
    def _ensure_unique_ids(rows: tuple[LogicalRow, ...], source_name: str) -> None:
        ids = [row.get("id") for row in rows]
        if any(value is None for value in ids) or len(set(ids)) != len(ids):
            _fail(
                ErrorCode.QUERY_PLAN_INVARIANT_VIOLATION,
                f"Source '{source_name}' returned missing or duplicate logical IDs for record assembly.",
                source_name=source_name,
            )


def _matches(expression: BoundFilterExpression | None, row: LogicalRow) -> bool:
    """Evaluate SQL-style three-valued logic; only TRUE passes a WHERE clause."""

    return _truth(expression, row) is True


def _truth(expression: BoundFilterExpression | None, row: LogicalRow) -> bool | None:
    if expression is None:
        return True
    if isinstance(expression, BoundPredicate):
        return _predicate_truth(expression, row.get(expression.field.name))
    if isinstance(expression, BoundAllExpression):
        return _and(_truth(item, row) for item in expression.expressions)
    if isinstance(expression, BoundAnyExpression):
        return _or(_truth(item, row) for item in expression.expressions)
    if isinstance(expression, BoundNotExpression):
        value = _truth(expression.expression, row)
        return None if value is None else not value
    raise AssertionError(f"Unknown filter expression: {expression!r}")


def _predicate_truth(predicate: BoundPredicate, actual: object | None) -> bool | None:
    op = predicate.operator
    if op is ComparisonOperator.IS_NULL:
        return actual is None
    if op is ComparisonOperator.IS_NOT_NULL:
        return actual is not None
    if actual is None:
        return None
    expected = predicate.value
    if op is ComparisonOperator.EQ:
        return actual == expected
    if op is ComparisonOperator.NE:
        return actual != expected
    if op is ComparisonOperator.IN:
        return actual in expected  # type: ignore[operator]
    if op is ComparisonOperator.NOT_IN:
        values = expected  # type: ignore[assignment]
        return None if any(value is None for value in values) else actual not in values
    if op is ComparisonOperator.CONTAINS:
        return str(expected) in str(actual)
    if op is ComparisonOperator.STARTS_WITH:
        return str(actual).startswith(str(expected))
    try:
        if op is ComparisonOperator.GT:
            return actual > expected  # type: ignore[operator]
        if op is ComparisonOperator.GTE:
            return actual >= expected  # type: ignore[operator]
        if op is ComparisonOperator.LT:
            return actual < expected  # type: ignore[operator]
        if op is ComparisonOperator.LTE:
            return actual <= expected  # type: ignore[operator]
    except TypeError:
        return False
    raise AssertionError(f"Unsupported predicate operator: {op!r}")


def _and(values: object) -> bool | None:
    unknown = False
    for value in values:  # type: ignore[union-attr]
        if value is False:
            return False
        unknown = unknown or value is None
    return None if unknown else True


def _or(values: object) -> bool | None:
    unknown = False
    for value in values:  # type: ignore[union-attr]
        if value is True:
            return True
        unknown = unknown or value is None
    return None if unknown else False


def _compare_rows(left: LogicalRow, right: LogicalRow, order_by: tuple[Any, ...]) -> int:
    for term in order_by:
        first, second = left.get(term.field.name), right.get(term.field.name)
        # Match PostgreSQL's default NULL placement: LAST for ASC, FIRST for DESC.
        if first is None and second is None:
            continue
        if first is None:
            return 1 if term.direction is SortDirection.ASC else -1
        if second is None:
            return -1 if term.direction is SortDirection.ASC else 1
        if first == second:
            continue
        try:
            comparison = -1 if first < second else 1
        except TypeError:
            comparison = -1 if repr(first) < repr(second) else 1
        return comparison if term.direction is SortDirection.ASC else -comparison
    return 0


def _fail(code: ErrorCode, message: str, *, source_name: str | None = None) -> None:
    raise QueryExecutionError(
        ErrorDetail(code=code, message=message, retryable=False, source_name=source_name)
    )
