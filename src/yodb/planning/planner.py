"""Build one executable baseline physical DAG from a resolved logical query."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from hashlib import sha256
import json

from ..catalog import LogicalType
from ..errors import ErrorCode, ErrorDetail, QueryError
from ..query.models import (
    BoundAllExpression,
    BoundFilterExpression,
    BoundPredicate,
    BoundSemanticPredicate,
    BoundQuery,
    ComparisonOperator,
)
from ..query.semantic import semantic_predicates
from ..semantic import ProviderInfo, SemanticPlanKind
from ..query.resolution import QuerySourceShape, ResolvedField, SingleSourceQueryBinding, SourceResolvedQuery
from .contracts import (
    CoordinatorFilter,
    CoordinatorSortPage,
    PhysicalPlan,
    PlanExplanation,
    PlanExplanationNode,
    PlanProperties,
    PlannedQuery,
    AssemblyStep,
    RecordAssembly,
    RemoteScan,
    ResultCompleteness,
    ResultProject,
    ResultShape,
    SemanticVerify,
    SourceOperationRequest,
    StepRole,
    VectorSearch,
    coordinator_location,
    default_schedule,
    remote_location,
)
from .capabilities import TextOrdering
from .optimizer import (
    Constraints,
    CostParameters,
    Fallback,
    OptimizerResult,
    Problem,
    SemanticOptions,
    SourceInput,
    optimize,
)
from .registry import SourcePlanningRegistry
from .statistics import StatisticsService


@dataclass(frozen=True)
class PlannerPolicy:
    """Hard baseline planner limits; runtime enforces the same scan guards."""

    maximum_rows_per_source: int = 10_000
    # Largest logical-ID set transferred between sources to restrict a later
    # scan (0 disables transfer).  Above this the executor runs a plain scan.
    maximum_transfer_keys: int = 1_000

    def __post_init__(self) -> None:
        if self.maximum_rows_per_source < 1:
            raise ValueError("maximum_rows_per_source must be positive")
        if self.maximum_transfer_keys < 0:
            raise ValueError("maximum_transfer_keys must not be negative")


class SemanticPlanPreference(str, Enum):
    AUTO = "auto"                          # shortlist when eligible, else verify all
    VERIFY_ALL = "verify_all"              # always Plan A
    VECTOR_SHORTLIST = "vector_shortlist"  # Plan B or a planning error


@dataclass(frozen=True)
class SemanticPolicy:
    """Planner limits and rules for semantic conditions.

    The plan choice here is a fixed rule (shortlist whenever it is eligible),
    not a cost estimate; statistics-driven choice is a later increment.
    """

    preference: SemanticPlanPreference = SemanticPlanPreference.AUTO
    maximum_candidates: int = 1_000     # most records ever sent to the verifier
    shortlist_oversample: int = 10      # shortlist >= page size x this
    minimum_shortlist: int = 20
    # What the configured EmbeddingProvider produces; None means no embedder.
    embedder: ProviderInfo | None = None
    embedder_dimensions: int | None = None

    def __post_init__(self) -> None:
        if min(self.maximum_candidates, self.shortlist_oversample, self.minimum_shortlist) < 1:
            raise ValueError("semantic limits must be positive")


class FederatedPhysicalPlanner:
    """Create a correct, executable baseline before enumerating alternatives."""

    def __init__(
        self,
        adapters: SourcePlanningRegistry,
        *,
        policy: PlannerPolicy = PlannerPolicy(),
        semantic: SemanticPolicy = SemanticPolicy(),
        statistics: StatisticsService | None = None,
        costs: CostParameters = CostParameters(),
    ) -> None:
        self._adapters = adapters
        self._policy = policy
        self._semantic = semantic
        self._statistics = statistics
        self._costs = costs

    def plan(self, resolved: SourceResolvedQuery) -> PlannedQuery:
        query = resolved.query
        if query.page.after is not None:
            _fail(
                ErrorCode.QUERY_FEATURE_NOT_SUPPORTED,
                "Cursor execution is unavailable until signed cursor verification is implemented.",
                "page.after",
            )
        semantic = semantic_predicates(query.where)
        # Everything except the semantic term is planned exactly as before; the
        # semantic term becomes a SemanticVerify node over the resulting records.
        core = resolved
        if semantic:
            core = replace(resolved, query=replace(query, where=_without_semantic(query.where)))
        scans, fully_pushed, page_pushed = self._build_scans(core, allow_complete=not semantic)
        choice = None
        if semantic:
            choice = self._choose_semantic_plan(core, semantic[0], scans, fully_pushed)

        # Fixed rules always give a correct plan.  With enough statistics the
        # optimizer may replace the read order and the semantic variant by a
        # cheaper one; otherwise it says why it declined.
        schedule: tuple = ()
        optimizer_notes: tuple[str, ...] = ()
        if semantic or resolved.shape is QuerySourceShape.MULTI_SOURCE:
            outcome = self._optimize(core, scans, choice, fully_pushed, semantic[0] if semantic else None, query)
            if isinstance(outcome, OptimizerResult):
                schedule = outcome.schedule
                choice = self._apply_semantic_decision(choice, outcome.semantic)
                optimizer_notes = (
                    "strategy=cost_based",
                    f"estimated_latency_ms={outcome.estimate.latency_ms:.1f}",
                    f"estimated_money={outcome.estimate.money:.6f}",
                    f"candidates_considered={outcome.candidates_considered}",
                )
            else:
                optimizer_notes = ("strategy=rules", f"reason={outcome.reason}")

        if choice is not None and choice.vector_search is not None:
            anchor, *rest = scans
            scans = (
                replace(anchor, vector_search=choice.vector_search, limit=choice.vector_search.shortlist_size, maximum_rows=None),
                *rest,
            )
        if resolved.shape is QuerySourceShape.SINGLE_SOURCE:
            current: PhysicalPlan = scans[0]
        else:
            current = self._assembly(scans, core, schedule)

        # The coordinator re-evaluates the filter unless every term was
        # accepted by its owning source (then the sources already enforced it
        # exactly; see ``_build_scans``).
        if not fully_pushed:
            current = CoordinatorFilter(
                input=current,
                expression=core.query.where,
                properties=_properties_from(
                    current.properties,
                    location=coordinator_location(),
                ),
            )
        if choice is not None:
            sem = semantic[0]
            current = SemanticVerify(
                input=current,
                field=next(f for scan in scans for f in scan.projection if f.field.name == sem.field.name),
                proposition=sem.proposition,
                plan=choice.kind,
                order_by=query.order_by,
                first=_effective_limit(query),
                minimum_quality=query.constraints.minimum_quality,
                maximum_candidates=self._semantic.maximum_candidates,
                maximum_cost=query.constraints.maximum_cost,
                maximum_latency_ms=query.constraints.maximum_latency_ms,
                embedding_model=None if choice.vector_search is None else choice.vector_search.model,
                embedding_dimensions=None if choice.vector_search is None else choice.vector_search.dimensions,
                properties=_properties_from(current.properties, location=coordinator_location()),
                choice_reasons=choice.reasons,
            )
        # When the one source enforced the exact order and page, re-sorting here
        # would substitute Python's string ordering for the source's collation.
        if not page_pushed:
            current = CoordinatorSortPage(
                input=current,
                order_by=query.order_by,
                first=query.page.first,
                after=query.page.after,
                notes=self._ordering_notes(query, scans),
                properties=_properties_from(
                    current.properties,
                    ordering=query.order_by,
                    location=coordinator_location(),
                ),
            )
        projection = _result_projection(query, scans)
        current = ResultProject(
            input=current,
            projection=projection,
            properties=PlanProperties(
                output_fields=projection,
                logical_id=_field_named("id", projection),
                ids_are_unique=True,
                ordering=current.properties.ordering,
                location=coordinator_location(),
                completeness=ResultCompleteness.EXACT,
                result_shape=ResultShape.RECORDS,
                catalog_fingerprint=query.catalog_fingerprint,
            ),
        )
        fingerprint = _plan_fingerprint(current)
        explanation = PlanExplanation(
            plan_kind="single_source" if resolved.shape is QuerySourceShape.SINGLE_SOURCE else "in_memory_record_assembly",
            catalog_fingerprint=query.catalog_fingerprint,
            plan_fingerprint=fingerprint,
            nodes=tuple(_explain_nodes(current)),
            optimizer=optimizer_notes,
        )
        return PlannedQuery(
            query=query,
            resolved=resolved,
            plan=current,
            catalog_fingerprint=query.catalog_fingerprint,
            query_fingerprint=query.query_fingerprint,
            plan_fingerprint=fingerprint,
            explain=explanation,
        )

    def _optimize(
        self,
        core: SourceResolvedQuery,
        scans: tuple[RemoteScan, ...],
        choice: "_SemanticChoice | None",
        fully_pushed: bool,
        semantic: BoundSemanticPredicate | None,
        query: BoundQuery,
    ) -> OptimizerResult | Fallback:
        if self._statistics is None:
            return Fallback("no statistics are configured")
        inputs: list[SourceInput] = []
        for index, scan in enumerate(scans):
            estimate = self._statistics.estimate_scan(scan.source, scan.pushed_filter)
            if not estimate.known or estimate.filtered_rows is None:
                return Fallback(f"statistics are unavailable for source '{scan.source.source_name}'")
            caps = self._adapters.adapter_for(scan.source.source_kind).capabilities
            role = StepRole.ANCHOR if index == 0 else (StepRole.REQUIRED if scan.pushed_filter is not None else StepRole.OPTIONAL)
            inputs.append(
                SourceInput(
                    name=scan.source.source_name,
                    role=role,
                    total_rows=estimate.total_rows,
                    filtered_rows=estimate.filtered_rows,
                    profile=estimate.profile,
                    key_limit=None if caps.key_lookup is None else caps.key_lookup.maximum_keys,
                    row_cap=self._row_cap(scan.source.source_kind) if scan.maximum_rows is not None else None,
                )
            )
        options = None
        if semantic is not None and choice is not None:
            anchor_caps = self._adapters.adapter_for(core.identity_source.source_kind).capabilities
            cap = self._semantic.maximum_candidates
            if anchor_caps.vector_search is not None and anchor_caps.vector_search.maximum_shortlist is not None:
                cap = min(cap, anchor_caps.vector_search.maximum_shortlist)
            shortlist_allowed = choice.vector_search is not None
            options = SemanticOptions(
                shortlist_allowed=shortlist_allowed,
                shortlist_start=min(choice.vector_search.shortlist_size, cap) if shortlist_allowed else 1,
                shortlist_cap=cap if shortlist_allowed else 1,
                maximum_candidates=self._semantic.maximum_candidates,
                page_size=_effective_limit(query),
                required_recall=(
                    query.constraints.minimum_quality
                    if query.constraints.minimum_quality is not None
                    else self._costs.minimum_expected_recall
                ),
                unpushed_selectivity=1.0 if fully_pushed else self._costs.unpushed_filter_selectivity,
                verify_all_allowed=self._semantic.preference is not SemanticPlanPreference.VECTOR_SHORTLIST,
            )
        problem = Problem(
            inputs,
            self._costs,
            maximum_transfer_keys=self._policy.maximum_transfer_keys or None,
            semantic=options,
            constraints=Constraints(
                maximum_money=query.constraints.maximum_cost,
                maximum_latency_ms=None if query.constraints.maximum_latency_ms is None else float(query.constraints.maximum_latency_ms),
            ),
        )
        rule_order = [s.source_name for s in default_schedule(scans[0], scans[1:], tuple(
            scan.source.source_name for scan in scans[1:] if scan.pushed_filter is not None
        )) if s.role is not StepRole.OPTIONAL]
        return optimize(problem, rule_order=rule_order, rule_kind=None if choice is None else choice.kind)

    @staticmethod
    def _apply_semantic_decision(choice: "_SemanticChoice | None", decision) -> "_SemanticChoice | None":
        """Fold the optimizer's semantic variant into the rule choice (never widening what is legal)."""

        if choice is None or decision is None:
            return choice
        if decision.kind is SemanticPlanKind.VECTOR_SHORTLIST:
            assert choice.vector_search is not None  # the optimizer only offers it when legal
            vector = replace(choice.vector_search, shortlist_size=decision.shortlist_size)
            note = f"cost-based: shortlist of {decision.shortlist_size}"
            return _SemanticChoice(decision.kind, vector, (*choice.reasons, note))
        if choice.kind is SemanticPlanKind.VECTOR_SHORTLIST:
            return _SemanticChoice(decision.kind, None, ("cost-based: verifying every candidate is cheaper than a shortlist",))
        return choice

    def _row_cap(self, source_kind) -> int:
        """Most rows one scan may return: the policy bound, or the source's own if lower."""

        declared = self._adapters.adapter_for(source_kind).capabilities.maximum_rows
        return self._policy.maximum_rows_per_source if declared is None else min(self._policy.maximum_rows_per_source, declared)

    def _ordering_notes(self, query: BoundQuery, scans: tuple[RemoteScan, ...]) -> tuple[str, ...]:
        """Flag text ordering done by YoDb that the owning source would collate differently."""

        if len(scans) < 2:
            return ()
        owner = {f.field.name: f.source_name for scan in scans for f in scan.projection if f.field.name != "id"}
        differing = sorted(
            {
                owner[term.field.name]
                for term in query.order_by
                if term.field.spec.type in {LogicalType.STRING, LogicalType.TEXT} and term.field.name in owner
                and self._adapters.adapter_for(
                    next(s.source.source_kind for s in scans if s.source.source_name == owner[term.field.name])
                ).capabilities.text_ordering
                is TextOrdering.SOURCE_DEFINED
            }
        )
        if not differing:
            return ()
        return (
            "text is ordered by YoDb in code-point order; "
            f"the owning source's collation differs for: {', '.join(differing)}",
        )

    def _choose_semantic_plan(
        self,
        resolved: SourceResolvedQuery,
        semantic: BoundSemanticPredicate,
        scans: tuple[RemoteScan, ...],
        fully_pushed: bool,
    ) -> "_SemanticChoice":
        """Pick Plan A or B by a fixed eligibility rule and say why."""

        reasons: list[str] = []
        policy = self._semantic
        anchor = resolved.identity_source
        core_has_filter = resolved.query.where is not None
        binding = anchor.embeddings.get(semantic.field.name)
        if policy.preference is SemanticPlanPreference.VERIFY_ALL:
            reasons.append("preference is verify_all")
        else:
            if binding is None:
                reasons.append(
                    "the identity source has no embedding for the field"
                    if any(f.field.name == semantic.field.name for f in anchor.fields)
                    else "the text field is not owned by the identity source"
                )
            elif policy.embedder is None or policy.embedder_dimensions is None:
                reasons.append("no embedding provider is configured")
            elif policy.embedder.model != binding.model or policy.embedder_dimensions != binding.dimensions:
                reasons.append("the embedding provider does not match the stored embedding model/dimensions")
            vector_caps = self._adapters.adapter_for(anchor.source_kind).capabilities.vector_search
            if vector_caps is None:
                reasons.append("the source cannot do vector search")
            else:
                if binding is not None and binding.metric not in vector_caps.metrics:
                    reasons.append(f"the source cannot rank by '{binding.metric.value}' distance")
                if not vector_caps.combines_with_filters and (
                    core_has_filter or resolved.shape is QuerySourceShape.MULTI_SOURCE
                ):
                    reasons.append("the source cannot combine vector ranking with other filters or key restrictions")
            if not fully_pushed:
                reasons.append("a filter term is not enforced by its source, so a shortlist would precede it")
        if not reasons:
            first = _effective_limit(resolved.query)
            size = min(policy.maximum_candidates, max(policy.minimum_shortlist, first * policy.shortlist_oversample))
            if vector_caps.maximum_shortlist is not None:
                size = min(size, vector_caps.maximum_shortlist)
            return _SemanticChoice(
                SemanticPlanKind.VECTOR_SHORTLIST,
                VectorSearch(
                    column=binding.column,
                    metric=binding.metric,
                    model=binding.model,
                    dimensions=binding.dimensions,
                    shortlist_size=size,
                    fallback_maximum_rows=self._row_cap(anchor.source_kind),
                ),
                (f"shortlist of {size}",),
            )
        if policy.preference is SemanticPlanPreference.VECTOR_SHORTLIST:
            _fail(ErrorCode.QUERY_PLAN_UNSUPPORTED, "Vector shortlist is unavailable: " + "; ".join(reasons), "where")
        return _SemanticChoice(SemanticPlanKind.VERIFY_ALL, None, tuple(reasons))

    def _build_scans(
        self, resolved: SourceResolvedQuery, *, allow_complete: bool = True
    ) -> tuple[tuple[RemoteScan, ...], bool, bool]:
        """Return the scans, whether sources alone enforce the whole filter, and
        whether the single source also enforced the exact order and page.

        That holds when the single source accepted the entire filter, or when
        the filter is a pure conjunction whose every leaf was accepted by its
        owning source (a contributor leaf also makes it a required match, so
        the assembly drops anchor rows that fail it).  Otherwise the
        coordinator must evaluate the original expression.
        """

        source_filters = _source_local_filters(resolved)
        complete = resolved.shape is QuerySourceShape.SINGLE_SOURCE and allow_complete
        total_leaves = _conjunctive_predicates(resolved.query.where)
        pushed_leaves = 0
        every_source_accepted = True
        page_pushed = False
        scans = []
        for source in resolved.sources:
            projection = _source_projection(source)
            requested = SourceOperationRequest(
                projection=projection,
                filter=resolved.query.where if complete else source_filters.get(source.source_name),
                order_by=resolved.query.order_by if complete else (),
                limit=_effective_limit(resolved.query) if complete else None,
                complete_result=complete,
            )
            decision = self._adapters.adapter_for(source.source_kind).plan_remote_scan(source, requested)
            if decision.accepted_projection != projection:
                _fail(
                    ErrorCode.SOURCE_CAPABILITY_UNAVAILABLE,
                    "The source planning adapter cannot provide the mandatory logical ID and projection.",
                    source.source_name,
                )
            if requested.filter is not None:
                if decision.accepted_filter is not requested.filter:
                    every_source_accepted = False
                else:
                    pushed_leaves += len(_conjunctive_predicates(requested.filter) or ())
            if complete:
                page_pushed = (
                    decision.accepted_filter is requested.filter
                    and decision.accepted_order == requested.order_by
                    and decision.accepted_limit == requested.limit
                )
            caps = self._adapters.adapter_for(source.source_kind).capabilities
            row_cap = self._row_cap(source.source_kind)
            needs_guard = not complete or decision.accepted_limit != requested.limit
            scans.append(
                RemoteScan(
                    source=source,
                    projection=projection,
                    pushed_filter=decision.accepted_filter,
                    order_by=decision.accepted_order,
                    limit=decision.accepted_limit,
                    maximum_rows=row_cap if needs_guard else None,
                    key_lookup_limit=None if caps.key_lookup is None else caps.key_lookup.maximum_keys,
                    properties=PlanProperties(
                        output_fields=projection,
                        logical_id=source.logical_id,
                        ids_are_unique=True,
                        ordering=decision.accepted_order if decision.accepted_order else None,
                        location=remote_location(source.source_name),
                        completeness=ResultCompleteness.EXACT,
                        result_shape=ResultShape.RECORDS,
                        catalog_fingerprint=resolved.query.catalog_fingerprint,
                    ),
                )
            )
        if complete:
            fully_pushed = every_source_accepted
        else:
            fully_pushed = (
                total_leaves is not None
                and every_source_accepted
                and pushed_leaves == len(total_leaves)
            )
        return tuple(scans), fully_pushed, page_pushed

    def _assembly(
        self,
        scans: tuple[RemoteScan, ...],
        resolved: SourceResolvedQuery,
        schedule: tuple[AssemblyStep, ...] = (),
    ) -> RecordAssembly:
        anchor, *contributors = scans
        fields = _deduplicate_fields(field for scan in scans for field in scan.projection)
        required_names = tuple(scan.source.source_name for scan in contributors if scan.pushed_filter is not None)
        return RecordAssembly(
            anchor=anchor,
            contributors=tuple(contributors),
            # A contributor scan with a pushed top-level AND conjunct is a
            # candidate-ID restriction as well as an enrichment source.  The
            # executor must retain only anchor records present in that scan.
            # This keeps a non-returned contributor from being mistaken for a
            # logical NULL during the defensive residual evaluation.
            required_contributor_matches=required_names,
            properties=PlanProperties(
                output_fields=fields,
                logical_id=anchor.properties.logical_id,
                ids_are_unique=True,
                ordering=None,
                location=coordinator_location(),
                completeness=ResultCompleteness.EXACT,
                result_shape=ResultShape.RECORDS,
                catalog_fingerprint=resolved.query.catalog_fingerprint,
            ),
            # Each scan carries its own source's limit; 0 disables transfer.
            maximum_transfer_keys=self._policy.maximum_transfer_keys or None,
            schedule=schedule or default_schedule(anchor, tuple(contributors), required_names),
        )


@dataclass(frozen=True)
class _SemanticChoice:
    kind: SemanticPlanKind
    vector_search: VectorSearch | None
    reasons: tuple[str, ...]


def _without_semantic(expression: BoundFilterExpression | None) -> BoundFilterExpression | None:
    """Drop the semantic conjunct (placement was validated to be conjunctive)."""

    if expression is None or isinstance(expression, BoundPredicate):
        return expression
    if isinstance(expression, BoundSemanticPredicate):
        return None
    if isinstance(expression, BoundAllExpression):
        kept = tuple(item for item in (_without_semantic(child) for child in expression.expressions) if item is not None)
        if not kept:
            return None
        return kept[0] if len(kept) == 1 else BoundAllExpression(kept)
    raise AssertionError(f"A semantic term cannot sit under {type(expression).__name__}")


def _source_projection(source: SingleSourceQueryBinding) -> tuple[ResolvedField, ...]:
    """Always include identity; contributor source.fields may not contain it."""

    return _deduplicate_fields((source.logical_id, *source.fields))


def _source_local_filters(resolved: SourceResolvedQuery) -> dict[str, BoundFilterExpression | None]:
    """Split only top-level conjunction leaves into source-owned fragments."""

    predicates = _conjunctive_predicates(resolved.query.where)
    if predicates is None:
        return {source.source_name: None for source in resolved.sources}
    # ``id`` is owned by the configured identity source.  Every participant
    # also has a physical representation of it purely so record assembly can
    # link rows; that must not accidentally change logical field ownership.
    fields_to_source = {
        field.field.name: source.source_name
        for source in resolved.sources
        for field in source.fields
    }
    fields_to_source["id"] = resolved.identity_source.source_name
    grouped: dict[str, list[BoundPredicate]] = {source.source_name: [] for source in resolved.sources}
    for predicate in predicates:
        source_name = fields_to_source.get(predicate.field.name)
        if source_name is None:
            continue
        # A contributor row that is absent enriches as all-NULL, so an IS NULL
        # test is TRUE for it.  Pushing that test would hide contributor rows
        # whose value is non-null and make them look NULL after assembly, so
        # it must stay in the coordinator residual only.
        if source_name != resolved.identity_source.source_name and predicate.operator is ComparisonOperator.IS_NULL:
            continue
        grouped[source_name].append(predicate)
    return {
        source_name: None
        if not source_predicates
        else source_predicates[0]
        if len(source_predicates) == 1
        else BoundAllExpression(tuple(source_predicates))
        for source_name, source_predicates in grouped.items()
    }


def _conjunctive_predicates(expression: BoundFilterExpression | None) -> tuple[BoundPredicate, ...] | None:
    if expression is None:
        return ()
    if isinstance(expression, BoundPredicate):
        return (expression,)
    if isinstance(expression, BoundAllExpression):
        children = tuple(_conjunctive_predicates(child) for child in expression.expressions)
        if any(child is None for child in children):
            return None
        return tuple(predicate for child in children for predicate in child or ())
    return None


def _effective_limit(query: BoundQuery) -> int:
    assert query.page.first is not None
    maximum = query.constraints.maximum_results
    return min(query.page.first, maximum) if maximum is not None else query.page.first


def _result_projection(query: BoundQuery, scans: tuple[RemoteScan, ...]) -> tuple[ResolvedField, ...]:
    # Retain the identity-source representation for logical ``id`` (and the
    # first declared owner for every other field) instead of letting a
    # contributor's physical ID mapping overwrite the logical property.
    fields: dict[str, ResolvedField] = {}
    for scan in scans:
        for field in scan.projection:
            fields.setdefault(field.field.name, field)
    return tuple(fields[field.name] for field in query.select)


def _field_named(name: str, fields: tuple[ResolvedField, ...]) -> ResolvedField:
    for field in fields:
        if field.field.name == name:
            return field
    raise AssertionError(f"Required logical field '{name}' is absent from plan projection")


def _deduplicate_fields(fields) -> tuple[ResolvedField, ...]:
    result: list[ResolvedField] = []
    seen: set[str] = set()
    for field in fields:
        if field.field.name not in seen:
            result.append(field)
            seen.add(field.field.name)
    return tuple(result)


def _properties_from(properties: PlanProperties, **changes) -> PlanProperties:
    values = {
        "output_fields": properties.output_fields,
        "logical_id": properties.logical_id,
        "ids_are_unique": properties.ids_are_unique,
        "ordering": properties.ordering,
        "location": properties.location,
        "completeness": properties.completeness,
        "result_shape": properties.result_shape,
        "catalog_fingerprint": properties.catalog_fingerprint,
    }
    values.update(changes)
    return PlanProperties(**values)


def _plan_fingerprint(plan: PhysicalPlan) -> str:
    encoded = json.dumps(_plan_shape(plan), sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _plan_shape(plan: PhysicalPlan) -> dict[str, object]:
    if isinstance(plan, RemoteScan):
        return {
            "kind": "remote_scan",
            "source": plan.source.source_name,
            "fields": [field.field.name for field in plan.projection],
            "filter": _filter_shape(plan.pushed_filter),
            "order": [(term.field.name, term.direction.value) for term in plan.order_by],
            "limit": plan.limit,
            "maximum_rows": plan.maximum_rows,
            "vector": None if plan.vector_search is None else [plan.vector_search.metric.value, plan.vector_search.shortlist_size],
        }
    if isinstance(plan, RecordAssembly):
        return {"kind": "record_assembly", "anchor": _plan_shape(plan.anchor), "contributors": [_plan_shape(item) for item in plan.contributors], "required_contributor_matches": list(plan.required_contributor_matches), "maximum_transfer_keys": plan.maximum_transfer_keys, "schedule": [[step.source_name, step.role.value, step.restrict] for step in plan.schedule]}
    if isinstance(plan, CoordinatorFilter):
        return {"kind": "coordinator_filter", "input": _plan_shape(plan.input), "filter": _filter_shape(plan.expression)}
    if isinstance(plan, SemanticVerify):
        return {"kind": "semantic_verify", "input": _plan_shape(plan.input), "field": plan.field.field.name, "plan": plan.plan.value, "first": plan.first, "maximum_candidates": plan.maximum_candidates, "minimum_quality": plan.minimum_quality is not None}
    if isinstance(plan, CoordinatorSortPage):
        return {"kind": "coordinator_sort_page", "input": _plan_shape(plan.input), "order": [(term.field.name, term.direction.value) for term in plan.order_by], "first": plan.first}
    if isinstance(plan, ResultProject):
        return {"kind": "result_project", "input": _plan_shape(plan.input), "fields": [field.field.name for field in plan.projection]}
    raise AssertionError(f"Unknown physical plan: {plan!r}")


def _filter_shape(expression: BoundFilterExpression | None) -> object:
    if expression is None:
        return None
    if isinstance(expression, BoundPredicate):
        return {"field": expression.field.name, "operator": expression.operator.value}
    if isinstance(expression, BoundAllExpression):
        return {"all": [_filter_shape(child) for child in expression.expressions]}
    name = type(expression).__name__.replace("Bound", "").replace("Expression", "").lower()
    child = getattr(expression, "expression", None)
    children = getattr(expression, "expressions", None)
    return {name: _filter_shape(child) if child is not None else [_filter_shape(item) for item in children]}


def _explain_nodes(plan: PhysicalPlan) -> list[PlanExplanationNode]:
    if isinstance(plan, RemoteScan):
        return [
            PlanExplanationNode(
                kind="remote_scan",
                location=plan.source.source_name,
                fields=tuple(field.field.name for field in plan.projection),
                pushed_filter_fields=_filter_fields(plan.pushed_filter),
                ordering=tuple(f"{term.field.name} {term.direction.value}" for term in plan.order_by),
                limit=plan.limit,
                detail=()
                if plan.vector_search is None
                else (f"vector_search={plan.vector_search.metric.value} top {plan.vector_search.shortlist_size}",),
            )
        ]
    if isinstance(plan, RecordAssembly):
        return [*_explain_nodes(plan.anchor), *[node for child in plan.contributors for node in _explain_nodes(child)], PlanExplanationNode("record_assembly", "coordinator", tuple(field.field.name for field in plan.properties.output_fields), key_transfer_max_keys=plan.maximum_transfer_keys, detail=("schedule: " + " -> ".join(f"{step.role.value}:{step.source_name}{'*' if step.restrict else ''}" for step in plan.schedule),))]
    if isinstance(plan, CoordinatorFilter):
        return [*_explain_nodes(plan.input), PlanExplanationNode("coordinator_filter", "coordinator", tuple(field.field.name for field in plan.properties.output_fields), residual_filter=plan.expression is not None)]
    if isinstance(plan, SemanticVerify):
        return [*_explain_nodes(plan.input), PlanExplanationNode("semantic_verify", "coordinator", tuple(field.field.name for field in plan.properties.output_fields), limit=plan.first, detail=(f"plan={plan.plan.value}", f"semantic_field={plan.field.field.name}", f"max_candidates={plan.maximum_candidates}", *(f"note: {reason}" for reason in plan.choice_reasons)))]
    if isinstance(plan, CoordinatorSortPage):
        return [*_explain_nodes(plan.input), PlanExplanationNode("coordinator_sort_page", "coordinator", tuple(field.field.name for field in plan.properties.output_fields), ordering=tuple(f"{term.field.name} {term.direction.value}" for term in plan.order_by), limit=plan.first, detail=tuple(f"note: {note}" for note in plan.notes))]
    if isinstance(plan, ResultProject):
        return [*_explain_nodes(plan.input), PlanExplanationNode("result_project", "coordinator", tuple(field.field.name for field in plan.projection))]
    raise AssertionError(f"Unknown physical plan: {plan!r}")


def _filter_fields(expression: BoundFilterExpression | None) -> tuple[str, ...]:
    if expression is None:
        return ()
    if isinstance(expression, BoundPredicate):
        return (expression.field.name,)
    if isinstance(expression, BoundAllExpression):
        return tuple(name for child in expression.expressions for name in _filter_fields(child))
    child = getattr(expression, "expression", None)
    children = getattr(expression, "expressions", None)
    return _filter_fields(child) if child is not None else tuple(name for item in children for name in _filter_fields(item))


def _fail(code: ErrorCode, message: str, location: str | None = None) -> None:
    raise QueryError(ErrorDetail(code=code, message=message, retryable=False, location=location))
