"""Handler for the JOIN operator: run the driving side, probe the other in key batches, match in memory."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cmp_to_key

from ...errors import ErrorCode, QueryExecutionError
from ...planning import HashJoin
from ...query.models import SortDirection
from ..contracts import LogicalRow
from .base import ExecutionContext, Run, fail
from .rows import compare_rows


@dataclass(frozen=True)
class _Field:
    name: str


@dataclass(frozen=True)
class _Term:
    """What ``compare_rows`` reads of an order term: a field name and a direction."""

    field: _Field
    direction: SortDirection


def execute(ctx: ExecutionContext, node: HashJoin, run: Run) -> tuple[LogicalRow, ...]:
    """The driving side runs once and gives its distinct join keys; the probing side runs per batch
    of keys, planned for that batch, under the same deadline; the rows are matched in memory, then
    ordered, paged and projected.

    Each side is an ordinary plan, run by the same executor, so its own guards, key transfer and
    extensions apply.  The join adds only its own bounds (driving rows, probing rows, joined rows).
    """

    policy = node.policy
    driver_rows = _side(ctx, node.driver, run)
    if len(driver_rows) > policy.maximum_driver_rows:
        fail(
            ErrorCode.QUERY_ROW_LIMIT_EXCEEDED,
            f"The {node.driver_side} side of the join produced more than {policy.maximum_driver_rows} rows; "
            "add a filter to one side of the join.",
        )
    keys = list(dict.fromkeys(row[node.driver_key] for row in driver_rows if row.get(node.driver_key) is not None))
    probe_rows: list[LogicalRow] = []
    for start in range(0, len(keys), policy.batch_size):
        _probe(ctx, node, keys[start:start + policy.batch_size], probe_rows, run)
    by_key: dict[object, list[LogicalRow]] = {}
    for row in probe_rows:
        by_key.setdefault(row[node.probe_key], []).append(row)

    joined: list[dict[str, object]] = []
    for driver_row in driver_rows:
        matches = by_key.get(driver_row.get(node.driver_key), ()) if driver_row.get(node.driver_key) is not None else ()
        if not matches and node.optional:
            joined.append(_merge(node, driver_row, None))   # a left join keeps the root row; its right fields are NULL
        for match in matches:
            joined.append(_merge(node, driver_row, match) if node.driver_side == "left" else _merge(node, match, driver_row))
        if len(joined) > policy.maximum_joined_rows:
            fail(
                ErrorCode.QUERY_COORDINATOR_LIMIT_EXCEEDED,
                f"The join produced more than {policy.maximum_joined_rows} rows; add a filter to one side.",
            )
    terms = [_Term(_Field(o.column), SortDirection.DESC if o.descending else SortDirection.ASC) for o in node.order]
    terms += [_Term(_Field("id"), SortDirection.ASC), _Term(_Field(f"{node.alias}.id"), SortDirection.ASC)]
    joined.sort(key=cmp_to_key(lambda a, b: compare_rows(a, b, tuple(terms))))
    columns = node.columns
    return tuple({name: row.get(name) for name in columns} for row in joined[: node.first])


def _probe(ctx: ExecutionContext, node: HashJoin, keys: list[object], collected: list[LogicalRow], run: Run) -> None:
    """Read the probing side for ``keys``.  A batch whose matches overflow a read guard (a few very popular keys)
    is split in two and each half read on its own: smaller key sets match fewer rows."""

    policy = node.policy
    run.remaining()                                        # the whole query's budget, between batches too
    budget = policy.maximum_probe_rows - len(collected)
    try:
        rows = _side(ctx, node.probe_for(keys, budget + 1), run)
    except QueryExecutionError as error:
        if error.code is ErrorCode.QUERY_ROW_LIMIT_EXCEEDED and len(keys) > 1:
            middle = len(keys) // 2
            _probe(ctx, node, keys[:middle], collected, run)
            _probe(ctx, node, keys[middle:], collected, run)
            return
        raise
    collected.extend(rows)
    if len(collected) > policy.maximum_probe_rows:
        fail(
            ErrorCode.QUERY_COORDINATOR_LIMIT_EXCEEDED,
            f"The other side of the join matched more than {policy.maximum_probe_rows} rows; add a filter to one side.",
        )


def _side(ctx: ExecutionContext, plan, run: Run) -> tuple[LogicalRow, ...]:
    """Run one side as its own query, sharing the deadline and the trace but not the per-query flags."""

    return ctx.execute(plan, Run(run.timeout_seconds, run.trace, started=run.started))


def _merge(node: HashJoin, left: LogicalRow, right: LogicalRow | None) -> dict[str, object]:
    row: dict[str, object] = dict(left)
    # Every column of the traversed side is present (NULL when unmatched), including those only used to order.
    names = set(node.right_columns) | {o.column[len(node.alias) + 1:] for o in node.order if o.column.startswith(node.alias + ".")}
    for name in names | {"id"}:
        row[f"{node.alias}.{name}"] = None if right is None else right.get(name)
    return row
