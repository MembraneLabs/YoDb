"""SEMANTIC_FILTER planning: the strategies for answering "is this proposition true?".

Everything semantic about planning lives here: the policy, the cost assumptions,
the fixed rule that chooses between verifying every candidate and verifying a
ranked shortlist, the cost variants the optimizer weighs, and the physical node
that is built.  The planner, the optimizer and the plan contracts never mention
a semantic condition.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import math
from typing import ClassVar, Protocol

from ..errors import ErrorCode, ErrorDetail, QueryError
from ..operators import OperatorKind
from ..query.models import BoundFilterExpression, BoundQuery
from ..query.resolution import QuerySourceShape, SingleSourceQueryBinding, SourceResolvedQuery
from ..query.semantic import BoundSemanticPredicate, semantic_predicates, without_semantic_terms
from ..query.models import BoundOrderTerm
from ..query.resolution import ResolvedField
from .contracts import ProviderCost, ProviderInfo, SemanticPlanKind
from ..planning.capabilities import OperatorSupport, SourceCapabilities, SupportLevel, support_view
from ..planning.contracts import (
    PhysicalNode,
    PhysicalPlan,
    PlanExplanationNode,
    PlanProperties,
    RemoteScan,
    ResultCompleteness,
    ResultShape,
    UnaryNode,
    VectorSearch,
    coordinator_location,
    properties_from,
    remote_location,
)
from ..planning.optimizer import Candidates, Estimate, RankedRead, Variant
from ..planning.operators.base import Claim, ExtensionPlan, PlanningServices, Strategy, effective_limit


class SemanticPlanPreference(str, Enum):
    AUTO = "auto"                          # let the planner choose among the legal strategies
    VERIFY_ALL = "verify_all"              # always verify every candidate
    VECTOR_SHORTLIST = "vector_shortlist"  # shortlist, or a planning error if it is unavailable


@dataclass(frozen=True)
class SemanticPolicy:
    """Planner limits and rules for semantic conditions."""

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


class RecallModel(Protocol):
    """Expected fraction of true matches a shortlist of ``shortlist`` rows finds.

    ``pool`` is how many rows the ranking chooses from.  Return 1.0 when the
    shortlist covers the pool.  Swap this for a model fitted to real data.
    """

    def expected_recall(self, shortlist: float, pool: float) -> float: ...


@dataclass(frozen=True)
class PowerLawRecall:
    """``(K / pool) ** exponent``: exponent 1 is a random ranking, 0 is perfect."""

    exponent: float = 0.5

    def __post_init__(self) -> None:
        if not (math.isfinite(self.exponent) and self.exponent >= 0):
            raise ValueError("exponent must be a finite non-negative number")

    def expected_recall(self, shortlist: float, pool: float) -> float:
        if pool <= shortlist or pool <= 0:
            return 1.0
        return (shortlist / pool) ** self.exponent


@dataclass(frozen=True)
class SemanticCosts:
    """What the cost model assumes about verifying and ranking (tunable, or taken from the providers)."""

    verification: ProviderCost = ProviderCost(
        money_per_call=0.0, money_per_candidate=0.001, latency_ms_per_call=300.0, latency_ms_per_candidate=20.0
    )
    embedding: ProviderCost = ProviderCost(money_per_call=0.0001, latency_ms_per_call=80.0)
    verifier_batch_size: int = 10
    # Fraction of candidates expected to satisfy the proposition (sets how soon a page fills).
    expected_selectivity: float = 0.1
    # Recall a shortlist must be expected to reach when the caller gave no quality bar.
    minimum_expected_recall: float = 0.8
    # Extra cost of ranking one row by vector distance.
    vector_ranking_per_row_ms: float = 0.002
    recall_model: RecallModel = field(default_factory=PowerLawRecall)

    def __post_init__(self) -> None:
        if not (math.isfinite(self.vector_ranking_per_row_ms) and self.vector_ranking_per_row_ms >= 0):
            raise ValueError("vector_ranking_per_row_ms must be a finite non-negative number")
        if not 0.0 < self.expected_selectivity <= 1.0:
            raise ValueError("expected_selectivity must be within (0, 1]")
        if not 0.0 <= self.minimum_expected_recall <= 1.0:
            raise ValueError("minimum_expected_recall must be within [0, 1]")
        if self.verifier_batch_size < 1:
            raise ValueError("verifier_batch_size must be positive")


@dataclass(frozen=True)
class SemanticVerify(UnaryNode):
    """Keep the input records for which the proposition is true.

    Candidates are ordered by ``order_by`` and verified in batches until
    ``first`` records qualify, so a page that fills early costs fewer model
    calls.  ``plan`` records whether the input was shortlisted by vector search.
    """

    operator: ClassVar[OperatorKind] = OperatorKind.SEMANTIC_FILTER

    input: PhysicalPlan
    field: ResolvedField
    proposition: str
    plan: SemanticPlanKind
    order_by: tuple[BoundOrderTerm, ...]
    first: int | None
    minimum_quality: float | None
    maximum_candidates: int
    maximum_cost: float | None
    maximum_latency_ms: int | None
    embedding_model: str | None
    embedding_dimensions: int | None
    properties: PlanProperties
    # Why this plan was chosen (e.g. why a shortlist was unavailable).
    choice_reasons: tuple[str, ...] = ()

    def shape(self) -> dict[str, object]:
        return {
            "kind": "semantic_verify",
            "input": self.input.shape(),
            "field": self.field.field.name,
            "plan": self.plan.value,
            "first": self.first,
            "maximum_candidates": self.maximum_candidates,
            "minimum_quality": self.minimum_quality is not None,
        }

    def describe(self) -> PlanExplanationNode:
        return PlanExplanationNode(
            "semantic_verify",
            "coordinator",
            tuple(field.field.name for field in self.properties.output_fields),
            limit=self.first,
            detail=(
                f"plan={self.plan.value}",
                f"semantic_field={self.field.field.name}",
                f"max_candidates={self.maximum_candidates}",
                *(f"note: {reason}" for reason in self.choice_reasons),
            ),
        )


@dataclass(frozen=True)
class SemanticOptions:
    """What the planner knows about answering the semantic term (inputs to the cost variants)."""

    shortlist_allowed: bool       # a shortlist is legal (eligible, the anchor owns the text...)
    shortlist_start: int          # smallest shortlist worth considering
    shortlist_cap: int            # largest allowed (candidate cap and the source's own limit)
    maximum_candidates: int       # more than this reaching the verifier fails at run time
    page_size: int
    required_recall: float
    unpushed_selectivity: float = 1.0   # < 1 when part of the filter still runs in YoDb
    verify_all_allowed: bool = True     # False when the caller insisted on a shortlist
    ranked_by_store: bool = False       # the vectors live in a separate store, read before the anchor

    def __post_init__(self) -> None:
        if min(self.shortlist_start, self.shortlist_cap, self.maximum_candidates, self.page_size) < 1:
            raise ValueError("semantic sizes must be positive")


@dataclass(frozen=True)
class SemanticDecision:
    """The payload of a semantic variant: which plan kind, and the shortlist size if any."""

    kind: SemanticPlanKind
    shortlist_size: int | None


def shortlist_ladder(options: SemanticOptions) -> list[int]:
    """Shortlist sizes to try: the start, doubling, up to and including the cap."""

    sizes: list[int] = []
    size = options.shortlist_start
    while size < options.shortlist_cap:
        sizes.append(size)
        size *= 2
    sizes.append(options.shortlist_cap)
    return sorted(set(sizes))


def semantic_variants(options: SemanticOptions, costs: SemanticCosts) -> list[Variant]:
    """The cost variants of a semantic condition: verify everything, or each shortlist size."""

    variants: list[Variant] = []
    if options.verify_all_allowed:
        variants.append(
            Variant(
                "verify_all",
                cost=lambda candidates: _estimate(options, costs, candidates, None),
                payload=SemanticDecision(SemanticPlanKind.VERIFY_ALL, None),
            )
        )
    if options.shortlist_allowed:
        for size in shortlist_ladder(options):
            variants.append(
                Variant(
                    f"shortlist:{size}",
                    cost=lambda candidates, size=size: _estimate(options, costs, candidates, size),
                    demand=RankedRead(size, costs.vector_ranking_per_row_ms, via_store=options.ranked_by_store),
                    approximate=True,
                    payload=SemanticDecision(SemanticPlanKind.VECTOR_SHORTLIST, size),
                )
            )
    return variants


def _estimate(options: SemanticOptions, costs: SemanticCosts, candidates: Candidates, shortlist: int | None) -> Estimate | None:
    """Latency and money of verifying ``candidates`` (None if this variant cannot be used)."""

    pool = candidates.count * options.unpushed_selectivity
    if shortlist is None:
        if pool > options.maximum_candidates:
            return None                      # would exceed the candidate cap at run time
    elif costs.recall_model.expected_recall(float(shortlist), candidates.pool) < options.required_recall:
        return None                          # the shortlist is not expected to find enough
    # A page that fills early stops early: verification work is bounded by page / hit rate.
    verified = min(pool, options.page_size / costs.expected_selectivity)
    calls = math.ceil(verified / costs.verifier_batch_size) if verified > 0 else 0
    cost = costs.verification
    latency = calls * cost.latency_ms_per_call + verified * cost.latency_ms_per_candidate
    money = calls * cost.money_per_call + verified * cost.money_per_candidate
    if shortlist is not None:
        latency += costs.embedding.latency_ms_per_call
        money += costs.embedding.money_per_call
    return Estimate(latency, money)


@dataclass(frozen=True)
class _Rule:
    """The fixed-rule choice: always legal, and the fallback when costing declines."""

    kind: SemanticPlanKind
    vector_search: VectorSearch | None
    reasons: tuple[str, ...]
    store: SingleSourceQueryBinding | None = None     # the separate vector store, when the vectors are not in the anchor


class SemanticOperator:
    kind = OperatorKind.SEMANTIC_FILTER

    def __init__(
        self,
        services: PlanningServices,
        policy: SemanticPolicy = SemanticPolicy(),
        costs: SemanticCosts = SemanticCosts(),
    ) -> None:
        self._services = services
        self._policy = policy
        self._costs = costs

    def claim(self, where: BoundFilterExpression | None) -> Claim | None:
        terms = semantic_predicates(where)
        return Claim(terms, without_semantic_terms(where)) if terms else None

    def plan(self, claim: Claim, core: SourceResolvedQuery, scans, query: BoundQuery) -> ExtensionPlan:
        if len(claim.terms) > 1:
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.QUERY_FEATURE_NOT_SUPPORTED,
                    message="Execution supports one semantic condition per query.",
                    retryable=False,
                    location="where",
                )
            )
        term: BoundSemanticPredicate = claim.terms[0]  # type: ignore[assignment]
        rule = self._rule(core, term, scans.fully_pushed)
        options = self._options(core, rule, scans.fully_pushed, query)
        field = next(f for source in core.sources for f in source.fields if f.field.name == term.field.name)
        template = rule.vector_search
        store_scan = self._store_scan(rule, query)
        strategies = tuple(
            Strategy(
                variant=variant,
                prepare=self._preparer(variant.payload, template, store_scan),
                build=self._builder(term, field, query, variant.payload, template),
            )
            for variant in semantic_variants(options, self._costs)
        )
        wanted = SemanticDecision(
            rule.kind, None if rule.vector_search is None else rule.vector_search.shortlist_size
        )
        default = next(s for s in strategies if s.variant.payload == wanted)

        def notes_for(chosen: Strategy | None) -> tuple[str, ...]:
            if chosen is None:
                return rule.reasons
            decision: SemanticDecision = chosen.variant.payload  # type: ignore[assignment]
            if decision.kind is SemanticPlanKind.VECTOR_SHORTLIST:
                return (*rule.reasons, f"cost-based: shortlist of {decision.shortlist_size}")
            if rule.kind is SemanticPlanKind.VECTOR_SHORTLIST:
                return ("cost-based: verifying every candidate is cheaper than a shortlist",)
            return rule.reasons

        return ExtensionPlan(self.kind, strategies, default, notes_for)

    # --- the fixed rule ------------------------------------------------------------

    def _binding(self, core: SourceResolvedQuery, term: BoundSemanticPredicate):
        """Where the field's vectors live: next to the text, or in a separate vector store."""

        binding = core.identity_source.embeddings.get(term.field.name)
        if binding is not None:
            return binding, None
        for store in core.extension_sources:
            binding = store.embeddings.get(term.field.name)
            if binding is not None:
                return binding, store
        return None, None

    def _rule(self, core: SourceResolvedQuery, term: BoundSemanticPredicate, fully_pushed: bool) -> _Rule:
        """Pick verify-all or a shortlist by a fixed eligibility rule and say why."""

        services, policy = self._services, self._policy
        reasons: list[str] = []
        anchor = core.identity_source
        binding, store = self._binding(core, term)
        ranking_source = anchor if store is None else store
        vector_caps = services.capabilities(ranking_source.source_kind).vector_search
        if policy.preference is SemanticPlanPreference.VERIFY_ALL:
            reasons.append("preference is verify_all")
        else:
            if binding is None:
                reasons.append(
                    "no source holds embeddings for the field"
                    if any(f.field.name == term.field.name for f in anchor.fields)
                    else "the text field is not owned by the identity source"
                )
            elif policy.embedder is None or policy.embedder_dimensions is None:
                reasons.append("no embedding provider is configured")
            elif policy.embedder.model != binding.model or policy.embedder_dimensions != binding.dimensions:
                reasons.append("the embedding provider does not match the stored embedding model/dimensions")
            if vector_caps is None:
                reasons.append("the source cannot do vector search")
            else:
                if binding is not None and binding.metric not in vector_caps.metrics:
                    reasons.append(f"the source cannot rank by '{binding.metric.value}' distance")
                needs_combining = core.query.where is not None or core.shape is QuerySourceShape.MULTI_SOURCE
                if store is None and not vector_caps.combines_with_filters and needs_combining:
                    reasons.append("the source cannot combine vector ranking with other filters or key restrictions")
                if store is not None and not vector_caps.combines_with_filters and core.shape is QuerySourceShape.MULTI_SOURCE:
                    reasons.append("the vector store cannot combine ranking with key restrictions")
            if store is not None:
                anchor_caps = services.capabilities(anchor.source_kind)
                if anchor_caps.key_lookup is None:
                    reasons.append("the identity source cannot be restricted by IDs, so shortlisted IDs could not narrow its read")
                if not services.policy.maximum_transfer_keys:
                    reasons.append("key transfer is disabled, so shortlisted IDs could not narrow the identity source's read")
            if not fully_pushed:
                reasons.append("a filter term is not enforced by its source, so a shortlist would precede it")
        if not reasons:
            first = effective_limit(core.query)
            size = min(policy.maximum_candidates, max(policy.minimum_shortlist, first * policy.shortlist_oversample))
            if vector_caps.maximum_shortlist is not None:
                size = min(size, vector_caps.maximum_shortlist)
            if store is not None:
                size = min(size, self._store_cap(core))
            return _Rule(
                SemanticPlanKind.VECTOR_SHORTLIST,
                VectorSearch(
                    column=binding.column,
                    metric=binding.metric,
                    model=binding.model,
                    dimensions=binding.dimensions,
                    shortlist_size=size,
                    fallback_maximum_rows=None if store is not None else services.row_cap(anchor.source_kind),
                ),
                (f"shortlist of {size}" if store is None else f"shortlist of {size} from vector store '{store.source_name}'",),
                store,
            )
        if policy.preference is SemanticPlanPreference.VECTOR_SHORTLIST:
            raise QueryError(
                ErrorDetail(
                    code=ErrorCode.QUERY_PLAN_UNSUPPORTED,
                    message="Vector shortlist is unavailable: " + "; ".join(reasons),
                    retryable=False,
                    location="where",
                )
            )
        return _Rule(SemanticPlanKind.VERIFY_ALL, None, tuple(reasons))

    def _store_cap(self, core: SourceResolvedQuery) -> int:
        """Most IDs a shortlist may return: the anchor must be restrictable by all of them."""

        services = self._services
        anchor_caps = services.capabilities(core.identity_source.source_kind)
        limits = [services.policy.maximum_transfer_keys, self._policy.maximum_candidates]
        if anchor_caps.key_lookup is not None:
            limits.append(anchor_caps.key_lookup.maximum_keys)
        return min(limit for limit in limits if limit)

    def _store_scan(self, rule: _Rule, query: BoundQuery) -> RemoteScan | None:
        """The read of the vector store: only IDs come back, ranked, at most K of them."""

        store = rule.store
        if store is None or rule.vector_search is None:
            return None
        caps = self._services.capabilities(store.source_kind)
        projection = (store.logical_id,)
        return RemoteScan(
            source=store,
            projection=projection,
            pushed_filter=None,
            order_by=(),
            limit=rule.vector_search.shortlist_size,
            maximum_rows=None,
            key_lookup_limit=None if caps.key_lookup is None else caps.key_lookup.maximum_keys,
            vector_search=rule.vector_search,
            properties=PlanProperties(
                output_fields=projection,
                logical_id=store.logical_id,
                ids_are_unique=True,
                ordering=None,
                location=remote_location(store.source_name),
                completeness=ResultCompleteness.EXACT,
                result_shape=ResultShape.RECORDS,
                catalog_fingerprint=query.catalog_fingerprint,
            ),
        )

    # --- what the cost model needs -------------------------------------------------

    def _options(self, core: SourceResolvedQuery, rule: _Rule, fully_pushed: bool, query: BoundQuery) -> SemanticOptions:
        services, policy = self._services, self._policy
        ranking_source = rule.store or core.identity_source
        ranking_caps = services.capabilities(ranking_source.source_kind)
        cap = policy.maximum_candidates
        if ranking_caps.vector_search is not None and ranking_caps.vector_search.maximum_shortlist is not None:
            cap = min(cap, ranking_caps.vector_search.maximum_shortlist)
        if rule.store is not None:
            cap = min(cap, self._store_cap(core))
        allowed = rule.vector_search is not None
        return SemanticOptions(
            shortlist_allowed=allowed,
            shortlist_start=min(rule.vector_search.shortlist_size, cap) if allowed else 1,
            shortlist_cap=cap if allowed else 1,
            maximum_candidates=policy.maximum_candidates,
            page_size=effective_limit(query),
            required_recall=(
                query.constraints.minimum_quality
                if query.constraints.minimum_quality is not None
                else self._costs.minimum_expected_recall
            ),
            unpushed_selectivity=1.0 if fully_pushed else services.costs.unpushed_filter_selectivity,
            verify_all_allowed=policy.preference is not SemanticPlanPreference.VECTOR_SHORTLIST,
            ranked_by_store=rule.store is not None,
        )

    # --- what a strategy does to the plan ------------------------------------------

    @staticmethod
    def _preparer(decision: SemanticDecision, template: VectorSearch | None, store_scan: RemoteScan | None = None):
        if decision.kind is not SemanticPlanKind.VECTOR_SHORTLIST:
            return lambda scans: scans
        search = replace(template, shortlist_size=decision.shortlist_size)
        if store_scan is not None:
            ranked = replace(store_scan, vector_search=search, limit=search.shortlist_size)

            def add_store(scans: tuple[RemoteScan, ...]) -> tuple[RemoteScan, ...]:
                return (*scans, ranked)

            return add_store

        def prepare(scans: tuple[RemoteScan, ...]) -> tuple[RemoteScan, ...]:
            anchor, *rest = scans
            return (replace(anchor, vector_search=search, limit=search.shortlist_size, maximum_rows=None), *rest)

        return prepare

    def _builder(self, term, field, query: BoundQuery, decision: SemanticDecision, template: VectorSearch | None):
        shortlist = decision.kind is SemanticPlanKind.VECTOR_SHORTLIST

        def build(node: PhysicalNode, notes: tuple[str, ...]) -> SemanticVerify:
            return SemanticVerify(
                input=node,
                field=field,
                proposition=term.proposition,
                plan=decision.kind,
                order_by=query.order_by,
                first=effective_limit(query),
                minimum_quality=query.constraints.minimum_quality,
                maximum_candidates=self._policy.maximum_candidates,
                maximum_cost=query.constraints.maximum_cost,
                maximum_latency_ms=query.constraints.maximum_latency_ms,
                embedding_model=template.model if shortlist else None,
                embedding_dimensions=template.dimensions if shortlist else None,
                properties=properties_from(node.properties, location=coordinator_location()),
                choice_reasons=notes,
            )

        return build


@support_view(OperatorKind.SEMANTIC_FILTER)
def _semantic_support(caps: SourceCapabilities) -> OperatorSupport:
    strategies = ("verify_all", "vector_shortlist") if caps.vector_search is not None else ("verify_all",)
    return OperatorSupport(
        OperatorKind.SEMANTIC_FILTER, SupportLevel.COORDINATOR, strategies, "YoDb verifies; a shortlist needs vector search"
    )
