"""Handler for the COMBINE operator: assemble one logical record from several sources."""

from __future__ import annotations

from dataclasses import replace

from ...errors import ErrorCode
from ...planning import RecordAssembly, RemoteScan, StepRole
from ..contracts import LogicalRow
from .base import ExecutionContext, Run, fail


def execute(ctx: ExecutionContext, node: RecordAssembly, run: Run) -> tuple[LogicalRow, ...]:
    """Read the sources in the plan's schedule order, then left-enrich the anchor.

    Anchor and *required* contributors bound the result: a record survives only
    if each of them returned it.  Every one of them therefore narrows the learned
    ID set (an intersection) that a later restricted read may use.  Optional
    contributors never narrow it; they only enrich.

    A *shortlist* step reads a vector store: the K IDs nearest the query, ranked
    among the IDs learned so far when they fit the store's lookup limit (else among
    all of its vectors).  The later reads are then restricted to those K IDs.

    A restricted read falls back to a plain guarded scan when the learned set
    exceeds what the source accepts even in batches (``maximum_key_batches`` reads of
    ``maximum_transfer_keys``), and an empty set ends the query without
    further source reads.  A ranked (shortlist) anchor read is only valid once
    every required contributor has narrowed it; otherwise the shortlist would be
    cut before the required matches and could come back short, so it falls back
    to a plain scan and Plan A verification.
    """

    scans = {node.anchor.source.source_name: node.anchor}
    scans.update({c.source.source_name: c for c in node.contributors})
    required_total = sum(1 for step in node.schedule if step.role is StepRole.REQUIRED)
    keys: set[object] | None = None
    required_done = 0
    required_ids: list[set[object]] = []
    enrichment: list[tuple[RemoteScan, tuple[LogicalRow, ...]]] = []
    anchor_rows: tuple[LogicalRow, ...] = ()
    for step in node.schedule:
        scan = scans[step.source_name]
        narrowing = step.role is not StepRole.OPTIONAL
        if step.role is StepRole.ANCHOR and scan.vector_search is not None:
            safe = required_done == required_total and (
                required_total == 0 or _can_restrict(scan, keys if step.restrict else None, node)
            )
            if not safe:
                run.fell_back = True
                scan = replace(scan, vector_search=None, limit=None, maximum_rows=scan.vector_search.fallback_maximum_rows)
        rows = _read(ctx, scan, keys if step.restrict else None, node.maximum_transfer_keys, node.maximum_key_batches, run)
        if step.role is StepRole.ANCHOR:
            anchor_rows = rows
        elif step.role is StepRole.SHORTLIST:
            pass                      # a ranking of IDs: it narrows, it never contributes fields
        else:
            enrichment.append((scan, rows))
        if step.role is StepRole.REQUIRED:
            required_done += 1
            required_ids.append({row["id"] for row in rows})
        elif step.role is StepRole.SHORTLIST:
            required_ids.append({row["id"] for row in rows})      # only shortlisted records may survive
        if narrowing:
            ids = {row["id"] for row in rows}
            keys = ids if keys is None else keys & ids
            if not keys:
                return ()

    records: dict[object, dict[str, object]] = {row["id"]: dict(row) for row in anchor_rows}
    for _, rows in enrichment:
        for row in rows:
            target = records.get(row["id"])
            if target is not None:
                # The identity is a join key, never a competing field value.
                target.update((name, value) for name, value in row.items() if name != "id")
    return tuple(record for logical_id, record in records.items() if all(logical_id in ids for ids in required_ids))


def _bound(scan: RemoteScan, maximum_keys: int | None) -> int | None:
    """The largest ID set this scan may be restricted by (None: never restricted)."""

    if maximum_keys is None or scan.key_lookup_limit is None:
        return None
    return min(maximum_keys, scan.key_lookup_limit)


def _can_restrict(scan: RemoteScan, keys: set[object] | None, node: RecordAssembly) -> bool:
    """Whether ``keys`` would actually narrow ``scan`` (its source accepts a set this size)."""

    bound = _bound(scan, node.maximum_transfer_keys)
    return keys is not None and bound is not None and len(keys) <= bound


def _read(
    ctx: ExecutionContext,
    scan: RemoteScan,
    keys: set[object] | None,
    maximum_keys: int | None,
    maximum_batches: int,
    run: Run,
) -> tuple[LogicalRow, ...]:
    """Execute one scan, restricted to ``keys`` when the source accepts a set that size.

    A set larger than one restriction may carry is sent in several restricted reads (up to
    ``maximum_batches``) and the rows are put together; the batches are disjoint, so nothing is
    counted twice.  Only a read that is neither ranked nor limited can be batched: cutting a
    ranking or a page into pieces would change its meaning.  Beyond the batches the scan runs
    unrestricted under its row guard, as before.
    """

    bound = _bound(scan, maximum_keys)
    if keys is not None and bound is not None and len(keys) <= bound:
        rows = ctx.execute(replace(scan, key_filter=tuple(sorted(keys, key=str))), run)
    elif (
        keys is not None
        and bound is not None
        and bound > 0
        and len(keys) <= bound * maximum_batches
        and scan.limit is None
        and scan.vector_search is None
    ):
        ordered = sorted(keys, key=str)
        rows_list: list[LogicalRow] = []
        for start in range(0, len(ordered), bound):
            run.remaining()
            rows_list.extend(ctx.execute(replace(scan, key_filter=tuple(ordered[start:start + bound])), run))
            if scan.maximum_rows is not None and len(rows_list) > scan.maximum_rows:
                fail(
                    ErrorCode.QUERY_ROW_LIMIT_EXCEEDED,
                    f"Source '{scan.source.source_name}' exceeded the V0.1 record-assembly guard of {scan.maximum_rows} rows.",
                    source_name=scan.source.source_name,
                )
        rows = tuple(rows_list)
    else:
        rows = ctx.execute(scan, run)
    ids = [row.get("id") for row in rows]
    if any(value is None for value in ids) or len(set(ids)) != len(ids):
        fail(
            ErrorCode.QUERY_PLAN_INVARIANT_VIOLATION,
            f"Source '{scan.source.source_name}' returned missing or duplicate logical IDs for record assembly.",
            source_name=scan.source.source_name,
        )
    return rows
