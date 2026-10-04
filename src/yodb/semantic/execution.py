"""Execution of the SEMANTIC_FILTER operator: verify candidates against a proposition."""

from __future__ import annotations

from dataclasses import replace
from functools import cmp_to_key
import time

from ..errors import ErrorCode, ErrorDetail, QueryExecutionError, YoDbError
from ..execution.contracts import LogicalRow
from ..execution.operators.base import ExecutionContext, Run, fail
from ..execution.operators.rows import compare_rows
from ..planning import RemoteScan, transform_plan
from .contracts import (
    EmbeddingRequest,
    SemanticExecutionStats,
    SemanticPlanKind,
    SemanticQueryReport,
    SemanticRecordMetadata,
    SemanticRuntime,
    VerificationCandidate,
    VerificationRequest,
    VerificationUsage,
    batches,
    passes_quality,
)
from .planning import SemanticVerify


def execute(runtime: SemanticRuntime | None, ctx: ExecutionContext, node: SemanticVerify, run: Run) -> tuple[LogicalRow, ...]:
    """Verify candidates in the caller's order until the page is full.

    Early stop is exact: the page is the first ``first`` qualifying records in
    ``order_by`` order, so verifying in that order and stopping when enough
    qualify returns the same page as verifying everything.
    """

    if runtime is None:
        fail(ErrorCode.SEMANTIC_PROVIDER_UNAVAILABLE, "No verification provider is configured.")
    started = time.perf_counter()
    input_plan = node.input
    embedding_calls = 0
    shortlisted: int | None = None
    if node.plan is SemanticPlanKind.VECTOR_SHORTLIST:
        if runtime.embedder is None:
            fail(ErrorCode.SEMANTIC_PROVIDER_UNAVAILABLE, "No embedding provider is configured.")
        input_plan = _with_query_vector(node.input, _embed(runtime.embedder, node, run))
        embedding_calls = 1
    rows = ctx.execute(input_plan, run)
    considered = len(rows)
    # If the ranked read could not be used safely the scan was a plain one, so
    # what actually ran (and what is reported) is verify-all.
    actual = SemanticPlanKind.VERIFY_ALL if run.fell_back else node.plan
    if actual is SemanticPlanKind.VECTOR_SHORTLIST:
        shortlisted = considered
    if considered > node.maximum_candidates:
        fail(
            ErrorCode.QUERY_SEMANTIC_BUDGET_EXCEEDED,
            f"{considered} candidates exceed the semantic limit of {node.maximum_candidates}; "
            "add filters or use a shortlist.",
        )
    name = node.field.field.name
    # A record with no text cannot be judged and never qualifies.
    candidates = [row for row in rows if isinstance(row.get(name), str) and row[name].strip()]
    if node.order_by:
        candidates.sort(key=cmp_to_key(lambda left, right: compare_rows(left, right, node.order_by)))
    by_id = {row["id"]: row for row in candidates}

    qualified: list[LogicalRow] = []
    records: dict[object, SemanticRecordMetadata] = {}
    usage = VerificationUsage()
    verified = 0
    info = runtime.verifier.info
    for batch in batches(tuple(VerificationCandidate(row["id"], row[name]) for row in candidates), runtime.batch_size):
        if node.first is not None and len(qualified) >= node.first:
            break
        if node.maximum_latency_ms is not None and (time.perf_counter() - started) * 1000 >= node.maximum_latency_ms:
            fail(ErrorCode.QUERY_SEMANTIC_BUDGET_EXCEEDED, "The semantic latency budget was exhausted.")
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
            fail(ErrorCode.QUERY_SEMANTIC_BUDGET_EXCEEDED, "The semantic cost budget was exceeded.")
        verdicts = {verdict.logical_id: verdict for verdict in result.verdicts}
        for candidate in batch:
            verdict = verdicts[candidate.logical_id]
            if passes_quality(verdict, node.minimum_quality):
                qualified.append(by_id[candidate.logical_id])
                records[candidate.logical_id] = SemanticRecordMetadata(
                    plan=actual, holds=True, confidence=verdict.confidence, info=info
                )
    if run.trace is not None:
        run.trace.reports["semantic"] = SemanticQueryReport(
            stats=SemanticExecutionStats(
                plan=actual,
                candidates_considered=considered,
                shortlisted=shortlisted,
                verified=verified,
                qualified=len(qualified),
                usage=usage,
                embedding_model_calls=embedding_calls,
            ),
            records=records,
        )
    return tuple(qualified)


def _embed(embedder, node: SemanticVerify, run: Run) -> tuple[float, ...]:
    try:
        result = embedder.embed(EmbeddingRequest((node.proposition,), run.timeout_seconds))
    except YoDbError:
        raise
    except Exception as error:
        raise QueryExecutionError(
            ErrorDetail(code=ErrorCode.SEMANTIC_PROVIDER_FAILED, message="The embedding provider failed.", retryable=False)
        ) from error
    if result.info.model != node.embedding_model or result.dimensions != node.embedding_dimensions or len(result.vectors) != 1:
        fail(
            ErrorCode.QUERY_PLAN_INVARIANT_VIOLATION,
            "The embedding provider does not match the stored embedding model and dimensions.",
        )
    return result.vectors[0]


def _with_query_vector(plan, vector: tuple[float, ...]):
    """Fill the embedded query vector into the one scan that searches by vector."""

    def fill(node):
        if isinstance(node, RemoteScan) and node.vector_search is not None:
            return replace(node, vector_search=replace(node.vector_search, query_vector=vector))
        return node

    return transform_plan(plan, fill)
