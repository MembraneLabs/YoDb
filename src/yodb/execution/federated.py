"""Execute V0.1 physical plans without leaking backend concerns upward.

The baseline deliberately favors correctness: remote fragments are executed
through the normal compiler/executor registries, then complete logical
semantics are enforced over normalized rows at the coordinator.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import cmp_to_key
import time
from typing import Any

from ..compilation import QueryCompilerRegistry
from ..errors import ErrorCode, ErrorDetail, QueryExecutionError, YoDbError
from ..planning import (
    CoordinatorFilter,
    CoordinatorSortPage,
    PhysicalPlan,
    RecordAssembly,
    RemoteScan,
    ResultProject,
    SemanticVerify,
    StepRole,
)
from ..semantic import (
    EmbeddingRequest,
    SemanticExecutionStats,
    SemanticPlanKind,
    SemanticRecordMetadata,
    SemanticRuntime,
    VerificationCandidate,
    VerificationRequest,
    VerificationUsage,
    batches,
    passes_quality,
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


@dataclass(frozen=True)
class ScanActual:
    """A scan that read its whole filtered result, and how many rows that was."""

    scan: RemoteScan
    rows: int


@dataclass
class ExecutionTrace:
    """What an execution observed beyond its rows (filled in by the executor)."""

    semantic_stats: SemanticExecutionStats | None = None
    semantic_records: dict[object, SemanticRecordMetadata] = field(default_factory=dict)
    scan_actuals: list[ScanActual] = field(default_factory=list)


@dataclass
class _Run:
    timeout_seconds: float | None
    trace: ExecutionTrace | None
    # Set when a planned shortlist could not be used safely and a plain scan ran.
    fell_back: bool = False


class FederatedPlanExecutor:
    """Interpret the small physical-plan vocabulary used by V0.1."""

    def __init__(
        self,
        compilers: QueryCompilerRegistry,
        executors: QueryExecutionAdapterRegistry,
        *,
        policy: FederatedExecutionPolicy = FederatedExecutionPolicy(),
        semantic: SemanticRuntime | None = None,
    ) -> None:
        self._compilers = compilers
        self._executors = executors
        self._policy = policy
        self._semantic = semantic

    def execute(
        self,
        plan: PhysicalPlan,
        *,
        timeout_seconds: float | None = None,
        trace: ExecutionTrace | None = None,
    ) -> tuple[LogicalRow, ...]:
        return self._execute(plan, _Run(timeout_seconds, trace))

    def _execute(self, plan: PhysicalPlan, run: _Run) -> tuple[LogicalRow, ...]:
        timeout_seconds = run.timeout_seconds
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
            if (
                run.trace is not None
                and plan.limit is None
                and plan.vector_search is None
                and plan.key_filter is None
            ):
                # A complete, unrestricted result: exactly what the planner wants to learn from.
                run.trace.scan_actuals.append(ScanActual(plan, len(rows)))
            return rows
        if isinstance(plan, RecordAssembly):
            return self._assemble(plan, run)
        if isinstance(plan, CoordinatorFilter):
            return tuple(row for row in self._execute(plan.input, run) if _matches(plan.expression, row))
        if isinstance(plan, SemanticVerify):
            return self._verify(plan, run)
        if isinstance(plan, CoordinatorSortPage):
            rows = list(self._execute(plan.input, run))
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
            rows = self._execute(plan.input, run)
            names = tuple(field.field.name for field in plan.projection)
            return tuple({name: row.get(name) for name in names} for row in rows)
        raise AssertionError(f"Unknown physical plan: {plan!r}")

    def _verify(self, node: SemanticVerify, run: _Run) -> tuple[LogicalRow, ...]:
        """Verify candidates in the caller's order until the page is full.

        Early stop is exact: the page is the first ``first`` qualifying records
        in ``order_by`` order, so verifying in that order and stopping when
        enough qualify returns the same page as verifying everything.
        """

        runtime = self._semantic
        if runtime is None:
            _fail(ErrorCode.SEMANTIC_PROVIDER_UNAVAILABLE, "No verification provider is configured.")
        started = time.perf_counter()
        input_plan = node.input
        embedding_calls = 0
        shortlisted: int | None = None
        if node.plan is SemanticPlanKind.VECTOR_SHORTLIST:
            if runtime.embedder is None:
                _fail(ErrorCode.SEMANTIC_PROVIDER_UNAVAILABLE, "No embedding provider is configured.")
            vector = self._embed(runtime.embedder, node, run)
            input_plan = _with_query_vector(node.input, vector)
            embedding_calls = 1
        rows = self._execute(input_plan, run)
        considered = len(rows)
        # If the ranked read could not be used safely the scan was a plain one,
        # so what actually ran (and what is reported) is verify-all.
        actual = SemanticPlanKind.VERIFY_ALL if run.fell_back else node.plan
        if actual is SemanticPlanKind.VECTOR_SHORTLIST:
            shortlisted = considered
        if considered > node.maximum_candidates:
            _fail(
                ErrorCode.QUERY_SEMANTIC_BUDGET_EXCEEDED,
                f"{considered} candidates exceed the semantic limit of {node.maximum_candidates}; "
                "add filters or use a shortlist.",
            )
        name = node.field.field.name
        # A record with no text cannot be judged and never qualifies.
        candidates = [row for row in rows if isinstance(row.get(name), str) and row[name].strip()]
        if node.order_by:
            candidates.sort(key=cmp_to_key(lambda left, right: _compare_rows(left, right, node.order_by)))
        by_id = {row["id"]: row for row in candidates}

        qualified: list[LogicalRow] = []
        usage = VerificationUsage()
        verified = 0
        info = runtime.verifier.info
        for batch in batches(
            tuple(VerificationCandidate(row["id"], row[name]) for row in candidates), runtime.batch_size
        ):
            if node.first is not None and len(qualified) >= node.first:
                break
            if node.maximum_latency_ms is not None and (time.perf_counter() - started) * 1000 >= node.maximum_latency_ms:
                _fail(ErrorCode.QUERY_SEMANTIC_BUDGET_EXCEEDED, "The semantic latency budget was exhausted.")
            request = VerificationRequest(node.proposition, batch, run.timeout_seconds)
            try:
                result = runtime.verifier.verify(request)
                result.require_complete_for(request)
            except YoDbError:
                raise
            except Exception as error:
                raise QueryExecutionError(
                    ErrorDetail(
                        code=ErrorCode.SEMANTIC_PROVIDER_FAILED,
                        message="The verification provider failed or returned an invalid result.",
                        retryable=False,
                    )
                ) from error
            usage = usage + result.usage
            info = result.info
            verified += len(batch)
            if node.maximum_cost is not None and usage.cost > node.maximum_cost:
                _fail(ErrorCode.QUERY_SEMANTIC_BUDGET_EXCEEDED, "The semantic cost budget was exceeded.")
            verdicts = {verdict.logical_id: verdict for verdict in result.verdicts}
            for candidate in batch:
                verdict = verdicts[candidate.logical_id]
                if passes_quality(verdict, node.minimum_quality):
                    qualified.append(by_id[candidate.logical_id])
                    if run.trace is not None:
                        run.trace.semantic_records[candidate.logical_id] = SemanticRecordMetadata(
                            plan=actual, holds=True, confidence=verdict.confidence, info=info
                        )
        if run.trace is not None:
            run.trace.semantic_stats = SemanticExecutionStats(
                plan=actual,
                candidates_considered=considered,
                shortlisted=shortlisted,
                verified=verified,
                qualified=len(qualified),
                usage=usage,
                embedding_model_calls=embedding_calls,
            )
        return tuple(qualified)

    @staticmethod
    def _embed(embedder, node: SemanticVerify, run: _Run) -> tuple[float, ...]:
        try:
            result = embedder.embed(EmbeddingRequest((node.proposition,), run.timeout_seconds))
        except YoDbError:
            raise
        except Exception as error:
            raise QueryExecutionError(
                ErrorDetail(
                    code=ErrorCode.SEMANTIC_PROVIDER_FAILED,
                    message="The embedding provider failed.",
                    retryable=False,
                )
            ) from error
        if result.info.model != node.embedding_model or result.dimensions != node.embedding_dimensions or len(result.vectors) != 1:
            _fail(
                ErrorCode.QUERY_PLAN_INVARIANT_VIOLATION,
                "The embedding provider does not match the stored embedding model and dimensions.",
            )
        return result.vectors[0]

    def _assemble(self, plan: RecordAssembly, run: _Run) -> tuple[LogicalRow, ...]:
        """Read the sources in the plan's schedule order, then left-enrich the anchor.

        Anchor and *required* contributors bound the result: a record survives
        only if each of them returned it.  Every one of them therefore narrows
        the learned ID set (an intersection) that a later restricted read may
        use.  Optional contributors never narrow it; they only enrich.

        A restricted read falls back to a plain guarded scan when the learned
        set exceeds the source's lookup limit, and an empty set ends the query
        without further source reads.  A ranked (shortlist) anchor read is only
        valid once every required contributor has narrowed it; otherwise the
        shortlist would be cut before the required matches and could come back
        short, so it falls back to a plain scan and Plan A verification.
        """

        scans = {plan.anchor.source.source_name: plan.anchor}
        scans.update({c.source.source_name: c for c in plan.contributors})
        required_total = sum(1 for step in plan.schedule if step.role is StepRole.REQUIRED)
        keys: set[object] | None = None
        required_done = 0
        required_ids: list[set[object]] = []
        enrichment: list[tuple[RemoteScan, tuple[LogicalRow, ...]]] = []
        anchor_rows: tuple[LogicalRow, ...] = ()
        for step in plan.schedule:
            scan = scans[step.source_name]
            narrowing = step.role is not StepRole.OPTIONAL
            if step.role is StepRole.ANCHOR and scan.vector_search is not None:
                safe = required_done == required_total and (
                    required_total == 0 or self._can_restrict(scan, keys if step.restrict else None, plan)
                )
                if not safe:
                    run.fell_back = True
                    scan = replace(
                        scan,
                        vector_search=None,
                        limit=None,
                        maximum_rows=scan.vector_search.fallback_maximum_rows,
                    )
            rows = self._scan(scan, keys if step.restrict else None, plan.maximum_transfer_keys, run)
            if step.role is StepRole.ANCHOR:
                anchor_rows = rows
            else:
                enrichment.append((scan, rows))
            if step.role is StepRole.REQUIRED:
                required_done += 1
                required_ids.append({row["id"] for row in rows})
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
        return tuple(
            record
            for logical_id, record in records.items()
            if all(logical_id in ids for ids in required_ids)
        )

    @staticmethod
    def _can_restrict(scan: RemoteScan, keys: set[object] | None, plan: RecordAssembly) -> bool:
        """Whether ``keys`` would actually narrow ``scan`` (its source accepts a set this size)."""

        bound = FederatedPlanExecutor._bound(scan, plan.maximum_transfer_keys)
        return keys is not None and bound is not None and len(keys) <= bound

    @staticmethod
    def _bound(scan: RemoteScan, maximum_keys: int | None) -> int | None:
        """The largest ID set this scan may be restricted by (None: never restricted)."""

        if maximum_keys is None or scan.key_lookup_limit is None:
            return None
        return min(maximum_keys, scan.key_lookup_limit)

    def _scan(
        self,
        scan: RemoteScan,
        keys: set[object] | None,
        maximum_keys: int | None,
        run: _Run,
    ) -> tuple[LogicalRow, ...]:
        """Execute one scan, restricted to ``keys`` when that set is small enough."""

        bound = self._bound(scan, maximum_keys)
        restricted = keys is not None and bound is not None and len(keys) <= bound
        if restricted:
            scan = replace(scan, key_filter=tuple(sorted(keys, key=str)))
        rows = self._execute(scan, run)
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


def _with_query_vector(plan: PhysicalPlan, vector: tuple[float, ...]) -> PhysicalPlan:
    """Fill the embedded query vector into the one scan that searches by vector."""

    if isinstance(plan, RemoteScan):
        if plan.vector_search is None:
            return plan
        return replace(plan, vector_search=replace(plan.vector_search, query_vector=vector))
    if isinstance(plan, RecordAssembly):
        return replace(plan, anchor=_with_query_vector(plan.anchor, vector))
    if isinstance(plan, CoordinatorFilter):
        return replace(plan, input=_with_query_vector(plan.input, vector))
    raise AssertionError(f"Unexpected node under SemanticVerify: {plan!r}")


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
