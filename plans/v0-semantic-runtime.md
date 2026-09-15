# V0 — Semantic Query Runtime

## Status and authority

This document records the focused semantic-runtime direction that informed
V0.1. [V0.1 — Federated Semantic Data Layer](v0.1-federated-semantic-data-layer.md)
is now the controlling delivery plan; this document remains a useful design
reference for `SemanticFilter`, semantic physical plans, and benchmarking.

V0 is not a universal database abstraction, agent-memory product, graph
database, or multi-database router. It is a focused experiment in optimizing
mixed deterministic and semantic queries.

## Product thesis

> YoDb is a semantic query runtime for deterministic software and agents that
> jointly optimizes database operations and probabilistic semantic operations
> under quality, latency, and cost constraints.

The V0 question is:

> Can YoDb execute mixed SQL + semantic predicates substantially more cheaply,
> quickly, and observably than evaluating an expensive model against every
> candidate row, while preserving acceptable quality?

The initial product wedge is:

> Run AI over operational data without blindly sending every row through an
> expensive model.

## V0 boundary

V0 has one canonical storage system and one vector implementation:

```text
PostgreSQL + pgvector
```

It has one model-provider interface. The provider can initially be a single
LLM/classifier service or local model, but the logical runtime must not embed a
provider-specific request shape in its query IR.

```text
Python SDK / structured agent tool
              |
              v
          Typed Query IR
              |
              v
       validator + optimizer
              |
       +------+------+
       |             |
       v             v
  PostgreSQL      model provider
   + pgvector    classifier / LLM
```

Agents and deterministic code are both clients of the same typed IR. Natural
language, when present, ends at the client-to-IR boundary; execution after that
boundary is deterministic and policy-controlled.

## Logical V0 algebra

The public query IR remains deliberately small:

```text
Scan
Filter
Project
Limit
SemanticFilter
```

`SemanticFilter(field, proposition)` answers whether a proposition is true of a
record. It is not merely a vector-nearest-neighbor request. For example:

```text
SemanticFilter(
  ticket.text,
  "The customer is seriously considering cancelling primarily because of price"
)
```

`VectorCandidateSearch` is an optional internal physical operator. It produces
a shortlist for a semantic filter; it is not the primary application-facing
abstraction.

The IR leaves room for a query execution contract:

```text
minimum quality
maximum latency
maximum cost
```

V0 uses simple explicit limits and heuristics. It does not need a learned cost
model, adaptive provisioning, or a large natural-language intent language.

## Required physical plans

One logical `SemanticFilter` must compile to at least two physical plans:

```text
Plan A — naive semantic evaluation

Postgres deterministic filters
        -> every candidate row
        -> model/classifier evaluation
```

```text
Plan B — retrieval-assisted semantic evaluation

Postgres deterministic filters
        -> pgvector/embedding shortlist
        -> model/classifier verification of the shortlist
```

Plan A is the quality/correctness baseline. Plan B is the optimization under
test. A future derived or materialized semantic value is a valid third physical
implementation, but it is not required to complete V0.

The planner estimates simple cost, latency, and quality for available plans,
then selects one that satisfies the query contract. It must expose the chosen
strategy rather than hiding model execution behind opaque behavior.

## Explainability and telemetry

`EXPLAIN AI` is a V0 feature. It must show:

- logical deterministic and semantic operators;
- candidate physical plans;
- input cardinality and shortlist size;
- estimated model evaluations, cost, latency, and quality;
- selected plan and the reason it satisfied the execution contract.

Every semantic execution records at least:

```text
query ID and operator ID
input/output cardinality
selected physical implementation
model and model version
tokens, latency, and monetary cost
confidence/scores
estimated versus observed cost and latency
quality when ground truth is available
```

This telemetry is product-critical. It supports benchmark comparisons in V0 and
later enables better planning, semantic materialization, and workload analysis.

## Graph and relationship support retained

The existing canonical relationship model remains supported in V0:

- PostgreSQL canonical edge tables;
- typed edge fields, versions, tombstones, cardinality, and endpoint checks;
- bounded, configurable, explicit multi-hop traversal;
- traversal-work budgets and canonical deletion checks.

Relationship traversal is a deterministic PostgreSQL capability in V0. It is
not the product thesis, a generic graph abstraction, or a prerequisite for the
semantic-filter experiment. Apache AGE, Neo4j, graph extraction, semantic joins,
and knowledge-graph construction remain future physical/operator work.

## Explicit V0 non-goals

Do not add these to V0:

```text
Qdrant, Neo4j, ClickHouse, or distributed query execution
generic multi-database adapter interfaces or automatic placement/migration
distributed transactions
agent framework, agent memory, or autonomous schema generation
knowledge-graph/entity/relation extraction
SemanticJoin, SemanticRank, SemanticMap, or SemanticClassify
adaptive semantic materialization
large natural-language intent language
```

The long-term logical-versus-physical separation, canonical-versus-derived
state model, backend capability model, and placement ideas remain valid. They
are infrastructure for later semantic operations, not the V0 product claim.

## Benchmark and success criteria

Use a realistic text-heavy dataset, such as customer-support tickets, product
reviews, contracts, or incident reports. The benchmark includes a structured
filter plus a semantic proposition and compares:

```text
1. model evaluation over all rows
2. SQL filter -> model evaluation
3. SQL filter -> pgvector shortlist -> model verification
```

Measure:

```text
precision, recall, F1/task quality
latency and monetary cost
model calls and token count
rows scanned and candidate reduction
planner estimates versus actual execution
```

V0 succeeds only when it demonstrates a material reduction in model cost and
latency with a small, measured, and acceptable quality loss compared with the
naive baseline. It is not enough for the runtime to be technically functional.

## Roadmap after V0

| Version | Focus |
| --- | --- |
| V1 | Additional semantic operators only where they enable meaningful planning alternatives. |
| V2 | Better model/retrieval strategies, statistics, evaluation, and agent structured-query tooling. |
| V3 | Adaptive semantic materialization with freshness, provenance, and definition versioning. |
| V4 | Extraction operators, entity resolution, and maintained knowledge. |
| V5 | Workload-aware knowledge extraction and agent-session/cross-query reuse. |
| V6+ | Additional databases when measured workloads justify them as physical targets. |

## Decision rule

If YoDb cannot materially beat a competent manually built pipeline such as
`SQL -> pgvector -> model verification`, or the quality loss required for cost
savings is unacceptable, do not expand database support. Revisit the product
direction instead.
