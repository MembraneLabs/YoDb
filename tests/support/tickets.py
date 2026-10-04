"""Shared test fixtures: tickets."""

from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path
from tempfile import TemporaryDirectory

from yodb.catalog import load_catalog
from yodb.inspection import SourceInspection, SourceValidationReport
from yodb.planning import (
    CostParameters,
    FederatedPhysicalPlanner,
    PlannerPolicy,
    PlanningServices,
    PostgresPlanningAdapter,
    SourcePlanningRegistry,
)
from yodb.query import bind_query, parse_query, QueryValidationPolicy, resolve_query_sources
from yodb.runtime import CatalogEvaluation, SourceRuntimeState, SourceRuntimeStatus
from yodb.semantic import (
    EmbeddingResult,
    ProviderInfo,
    SemanticExtension,
    SemanticPolicy,
    VerificationResult,
    VerificationUsage,
    VerificationVerdict,
)


DATASETS = """\
api_version: yodb/v0.1
catalog: {name: tickets, version: 1}
datasets:
  ticket:
    description: A support ticket.
    fields:
      id:       {type: id,     description: Identity.}
      subject:  {type: string, description: Subject.}
      body:     {type: text,   description: Ticket text., semantic_eligible: true}
      priority: {type: int,    description: Priority.}
      owner:    {type: string, description: Owner.}
      memo:     {type: text,   description: Owner memo., semantic_eligible: true}
"""


SOURCES = """\
api_version: yodb/v0.1
sources:
  helpdesk:
    kind: postgres
    connection_ref: helpdesk
    read_only: true
    datasets:
      ticket:
        resource: public.tickets
        identity: [id]
        fields:
          id: {physical_name: id}
          subject: {physical_name: subject}
          body: {physical_name: body}
          priority: {physical_name: priority}
        embeddings:
          body: {column: body_embedding, model: embed-v1, dimensions: 3, metric: cosine}
  directory:
    kind: postgres
    connection_ref: directory
    read_only: true
    datasets:
      ticket:
        resource: public.owners
        identity: [id]
        fields:
          id: {physical_name: ticket_id}
          owner: {physical_name: owner}
          memo: {physical_name: memo}
resolution:
  ticket:
    identity_source: helpdesk
    field_sources: {id: helpdesk, subject: helpdesk, body: helpdesk, priority: helpdesk, owner: directory, memo: directory}
"""


RELATIONS = """\
api_version: yodb/v0.1
relationships:
  ticket_reference:
    from: ticket
    to: ticket
    description: Placeholder relationship required by the loader.
    cardinality: one_to_one
    direction: uni
    implementations:
      - from: {source: helpdesk, field: id}
        to: {source: helpdesk, field: id}
"""


_NOW = datetime(2026, 10, 3, tzinfo=UTC)


EMBEDDER_INFO = ProviderInfo("fake", "embed-v1", "1")


JUDGE_INFO = ProviderInfo("fake", "judge", "1")


def ticket_catalog() -> CatalogEvaluation:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        for name, text in (("datasets", DATASETS), ("sources", SOURCES), ("relations", RELATIONS)):
            (root / f"{name}.yaml").write_text(text, encoding="utf-8")
        catalog = load_catalog(root)
    return CatalogEvaluation(
        catalog=catalog,
        evaluated_at=_NOW,
        sources={
            name: SourceRuntimeState(
                source_name=name,
                status=SourceRuntimeStatus.VALID,
                inspection=SourceInspection(source_name=name, source_kind=source.kind, inspected_at=_NOW),
                validation=SourceValidationReport(source_name=name, inspected_at=_NOW),
            )
            for name, source in catalog.sources.items()
        },
    )


class KeywordVerifier:
    """Holds iff the proposition's last word occurs in the text (deterministic)."""

    maximum_batch_size = 3

    def __init__(self, *, confidence=0.9, cost_per_candidate=0.01, fail=False, drop=False):
        self.info = JUDGE_INFO
        self.confidence = confidence
        self.cost = cost_per_candidate
        self.fail = fail
        self.drop = drop
        self.calls: list[list[object]] = []

    def verify(self, request):
        if self.fail:
            raise RuntimeError("provider down")
        self.calls.append([c.logical_id for c in request.candidates])
        needle = request.proposition.split()[-1].lower()
        verdicts = tuple(
            VerificationVerdict(
                c.logical_id,
                needle in c.text.lower(),
                self.confidence.get(c.logical_id, 0.9) if isinstance(self.confidence, dict) else self.confidence,
            )
            for c in request.candidates
        )
        if self.drop:
            verdicts = verdicts[:-1]
        usage = VerificationUsage(model_calls=1, input_tokens=10 * len(verdicts), cost=self.cost * len(request.candidates))
        return VerificationResult(verdicts, usage, JUDGE_INFO)


class FakeEmbedder:
    dimensions = 3

    def __init__(self, info=EMBEDDER_INFO, dimensions=3, fail=False):
        self.info = info
        self.dimensions = dimensions
        self.fail = fail
        self.requests = []

    def embed(self, request):
        if self.fail:
            raise RuntimeError("embed down")
        self.requests.append(request.texts)
        return EmbeddingResult(tuple((1.0, 0.0, 0.5) for _ in request.texts), self.info, self.dimensions)


def sem(proposition="mentions price", field="body"):
    return {"semantic": {"field": field, "proposition": proposition}}


def q(where, select=("subject",), first=10, order_by=None, **extra):
    raw = {"from": {"dataset": "ticket"}, "select": list(select), "where": where, "page": {"first": first}, **extra}
    if order_by:
        raw["order_by"] = order_by
    return raw


def prio(n=3):
    return {"field": "priority", "op": "gte", "value": n}


TICKETS = tuple(
    {"id": f"t{i}", "subject": f"S{i}", "body": body, "priority": 5}
    for i, body in enumerate(
        ["the PRICE is too high", "love it", "price hike again", "app crashed", "price and cancel", None, "  ", "fine"],
        start=1,
    )
)


ACTIVE = ticket_catalog()


OWNER = {"field": "owner", "op": "eq", "value": "ann"}


HELPDESK = tuple({"id": f"t{i}", "subject": f"S{i}", "body": f"price {i}", "priority": 5} for i in range(1, 7))


DIRECTORY = (
    {"id": "t2", "owner": "ann"}, {"id": "t4", "owner": "ann"}, {"id": "t6", "owner": "ann"},
)


def semantic_planner(semantic: SemanticPolicy | None = None, **kwargs) -> FederatedPhysicalPlanner:
    """A planner with the semantic filter plugged in (``kwargs`` go to the planner)."""

    return FederatedPhysicalPlanner(
        SourcePlanningRegistry([PostgresPlanningAdapter()]),
        extensions=(SemanticExtension(policy=semantic or SemanticPolicy()),),
        **kwargs,
    )


def plan(raw, *, policy=PlannerPolicy(), semantic=SemanticPolicy()):
    planner = semantic_planner(semantic, policy=policy)
    return planner.plan(resolve_query_sources(bind_query(parse_query(raw), ACTIVE), ACTIVE))


def resolve(raw, policy=QueryValidationPolicy()):
    """Parse, bind and resolve a ticket query against the ticket catalog."""

    return resolve_query_sources(bind_query(parse_query(raw), ACTIVE, policy=policy), ACTIVE)


def planning_services(*, statistics=None, policy=PlannerPolicy(), costs=CostParameters()):
    return PlanningServices(SourcePlanningRegistry([PostgresPlanningAdapter()]), policy, statistics, costs)
