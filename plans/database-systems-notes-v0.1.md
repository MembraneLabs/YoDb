# Database Systems Notes for YoDb V0.1

## Purpose

YoDb V0.1 is not building a general DBMS. It is building a read-only semantic
query layer that composes PostgreSQL relational reads, pgvector candidate
retrieval, optional Neo4j graph traversal, and model-based semantic
verification. The relevant database lessons are therefore not “replicate every
feature of Postgres,” but:

```text
express a logical request independently of execution
reduce data before expensive work
estimate the cost and risk of alternatives
keep source-local consistency honest
bound work, data movement, and failure modes
explain both chosen and rejected plans
measure actual execution to improve the next decision
```

This document separates established database mechanisms from the design choices
they imply for YoDb.

## 1. The database architecture YoDb should borrow

Most query systems divide work into four layers:

```text
logical algebra
    -> optimizer / planner
    -> physical plan
    -> executor
```

The logical algebra states *what* data is requested: scan a dataset, apply a
predicate, project fields, join, sort, or limit. The physical plan states *how*
to obtain it: sequential or index scan, hash or nested-loop join, local or
remote execution, materialization, and so on. PostgreSQL's `EXPLAIN` presents
the result as a tree of scan and higher-level operator nodes with estimated
cost, rows, and width; the planner minimizes the top-level plan cost.^1

System R established the core cost-based idea: a declarative query admits many
equivalent access paths, so a system should select among them using data
statistics and estimated cost.^2 Modern extensible optimizers such as Volcano
and Cascades generalize this by representing equivalent alternatives in a memo,
applying transformation and implementation rules, tracking physical properties
such as ordering, and pruning search with dynamic programming and bounds.^3 ^4

YoDb should use the same separation, but with a deliberately constrained search
space:

```text
LogicalQuery
  Scan(tickets)
  Filter(plan = 'enterprise')
  SemanticFilter(text, proposition)
  Traverse(authored_by -> member_of)

PhysicalPlan A
  PostgreSQL filter -> model verify every candidate

PhysicalPlan B
  PostgreSQL filter -> pgvector shortlist -> model verify -> hydrate

PhysicalPlan C
  PostgreSQL filter -> Neo4j traverse -> PostgreSQL hydrate
```

The logical query must never specify a table scan, HNSW parameter, Neo4j label,
SQL statement, Cypher statement, or model name. Those belong to the physical
plan and its explain output.

### V0.1 implication

Build a small typed IR and a small rule table, not a generic Cascades engine.
The important extensibility point is that one logical operator can map to more
than one physical implementation. The first such operator is `SemanticFilter`.

## 2. Logical rewrites versus physical alternatives

Database optimization has two distinct jobs that are often conflated.

### Logical rewrites

Logical rewrites preserve query meaning. Typical relational examples are
pushing a filter below a projection, combining filters, eliminating redundant
projections, and reordering inner joins when their predicates permit it. These
are generally rule-based and do not need a detailed cost model.

For YoDb, safe early rewrites include:

```text
Filter(A) then Filter(B)             -> one conjunctive Filter(A AND B)
Project(required fields) before join -> reduce transferred field width
Limit after ranking                  -> retain semantic correctness
Filter before SemanticFilter         -> reduce model candidates
Filter before Traverse               -> reduce graph frontier
```

The last two are high-value V0.1 rewrites. Running a model or graph traversal
before a selective deterministic filter is usually a direct cost mistake.

### Physical implementation choices

Physical choices need estimates. PostgreSQL can choose a sequential, index, or
bitmap scan; its estimates use table statistics and feed cost calculation.^1
YoDb similarly decides whether semantic evaluation should inspect all filtered
rows, use a pgvector shortlist, use a future materialized property, or fail a
budget. It also decides where a graph path runs: Postgres edge tables or Neo4j.

The distinction matters operationally:

```text
logical rewrite:    move plan = enterprise before SemanticFilter
physical choice:    exact pgvector scan vs HNSW shortlist vs no vector plan
```

Do not encode physical choices as logical IR flags. A request such as
`use_neo4j=true` or `hnsw_ef_search=200` hardens a short-lived implementation
detail into the user contract.

## 3. Cardinality estimation: the first optimizer capability

The most important planner value is not a sophisticated cost formula; it is a
reasonable estimate of how many items each step produces. PostgreSQL's planner
uses statistics specifically to estimate rows, which in turn supplies the raw
material for cost calculation.^5 Bad estimates select bad joins, wrong access
paths, and inappropriate data movement.

YoDb must estimate at least:

```text
rows in a source relation
filter selectivity
row width after projection
candidate count after vector retrieval
graph branching factor per relationship type
graph path expansion at each hop
model evaluations and average prompt/token size
remote round trips and transferred bytes
```

### Why estimates matter more in YoDb

An ordinary index mistake might turn milliseconds into seconds. A semantic-plan
mistake can turn 200 model calls into 200,000 model calls, changing both latency
and spend by orders of magnitude. A graph expansion can do the same through
fan-out. Therefore cardinality and hard work budgets are correctness-adjacent,
not just tuning details.

### V0.1 implementation

Start with explicit catalog statistics and conservative defaults:

```text
Postgres relation row count             from introspection/ANALYZE metadata
filter selectivity                      supplied statistic or conservative heuristic
vector shortlist size                  policy-controlled, e.g. min(10 * limit, cap)
graph branching factor                 measured per relationship/source binding
model input tokens per row             rolling observed average by field/model
```

Then record actual rows and update the catalog. Do not attempt learned
cardinality estimation initially. Instead, make estimates visible in `EXPLAIN
AI` and flag large estimate-versus-actual error ratios.

## 4. Cost models are multi-dimensional in semantic execution

Classic optimizers often collapse I/O and CPU into a scalar cost. PostgreSQL's
cost is expressed in configurable arbitrary units and deliberately excludes
client conversion/transmission time because the planner normally cannot alter
that part of a correct plan.^1 YoDb *can* alter network transfers, model calls,
and candidate counts, so its cost model must expose them.

For each physical operator, estimate a vector rather than only a scalar:

```text
cost_money       expected provider/model spend
latency          p50/p95 estimate, including source round trips
resource_cost    rows, bytes, CPU, graph expansion, tokens
quality          expected recall / proposition-verification quality
freshness        source read time and derived-index age
risk             probability of exceeding a limit or producing partial data
```

The planner first applies hard feasibility constraints:

```text
estimated model calls <= policy maximum
estimated graph expansion <= traversal budget
estimated transfer bytes <= policy maximum
estimated latency <= caller maximum, when provided
estimated quality >= caller minimum, when calibrated
```

It then ranks feasible plans. A useful initial lexicographic policy is:

```text
1. satisfy correctness, access, freshness, and hard budgets
2. maximize estimated quality
3. minimize monetary cost
4. minimize p95 latency
5. minimize transferred bytes / operational complexity
```

This is safer than inventing one weighted “score” before the team has evidence
for appropriate weights. Later, the ranking can become a configurable utility
function.

### Quality is not traditional selectivity

Semantic quality is measured, not assumed. A vector shortlist has recall with
respect to a labeled relevant set; a verifier has precision/recall or calibrated
confidence with respect to a proposition. The composition can lose relevant
items before verification ever sees them. V0.1 should store evaluation results
by dataset, proposition family, embedding model, verifier model, and shortlist
policy. Until enough labels exist, describe quality estimates as heuristics in
the explain output rather than guarantees.

## 5. Pushdown and data movement in federation

Federated execution adds a question a local optimizer does not face: where
should each operator run? Query shipping executes work at the data source; data
shipping moves records to the coordinator; hybrid plans mix both. Experiments
on client-server query processing found that neither pure policy is uniformly
best and that the choice is sensitive to system load and cache state.^6

PostgreSQL's foreign-data wrapper architecture demonstrates the same boundary:
the wrapper fetches remote data for the executor and participates in scan, join,
planning, `EXPLAIN`, `ANALYZE`, and asynchronous-execution hooks.^7 Federation
research also emphasizes that remote statistics are costly to obtain, so a
federated optimizer must gather useful cost information with few source
round-trips.^8

### V0.1 rules

1. Push a filter, projection, limit, ordering, or traversal hop to a source
   only when the adapter explicitly declares semantic equivalence.
2. Project join keys and required output fields before moving records.
3. Move the smaller candidate set across the source boundary.
4. Never silently perform an unbounded coordinator-side join or graph walk.
5. Every cross-source transfer has a row and byte cap visible in `EXPLAIN AI`.
6. Require an approved `JoinBinding`; matching field names are not evidence of
   identity.

For Postgres-to-Neo4j composition, a good default shape is:

```text
Postgres deterministic filter -> small source IDs -> Neo4j bounded traversal
-> small target IDs -> Postgres hydration
```

The inverse may be cheaper when graph filtering is more selective. That is a
future physical alternative once the planner has graph branching statistics.

## 6. Relational execution principles that directly matter

### Access paths

An index is not automatically better than a sequential scan. Selective queries
often favor indexes; broad queries can favor sequential scans because random
lookups and index traversal add overhead. PostgreSQL's `EXPLAIN` is the
practical ground truth for whether its planner chose a sequential, index, or
bitmap path and why.^1

YoDb should not override PostgreSQL's local access-path choice. It should emit
parameterized SQL, collect `EXPLAIN (ANALYZE, BUFFERS)` only in controlled
benchmark/diagnostic modes, and use aggregate measurements to decide only
YoDb-level choices such as source ordering or whether to invoke a model.

### Join order and join algorithm

Join order often dominates relational query cost because early selective joins
shrink later work. A nested loop is attractive for a small outer relation with
an indexed inner lookup; hash joins fit equi-joins with enough memory; merge
joins exploit compatible orderings. YoDb should let PostgreSQL select the local
algorithm. YoDb's responsibility is upstream: choose which source supplies the
small outer candidate set and whether the cross-source operation is allowed.

### Sort, limit, and top-k

`LIMIT` can change the useful cost of a plan because a parent may stop reading
its child early; PostgreSQL documents that node cost otherwise assumes complete
execution.^1 In YoDb, keep `LIMIT` close to ranking only when doing so preserves
semantics. A premature vector `LIMIT` can reduce proposition recall; a
post-verification `LIMIT` bounds returned results without reducing verification
coverage. This distinction must appear in the physical plan.

## 7. pgvector is an access path, not semantic truth

pgvector provides both exact nearest-neighbor search and approximate HNSW and
IVFFlat access paths. IVFFlat generally builds faster and uses less memory but
has a lower speed/recall trade-off than HNSW; index parameters determine that
trade-off.^9 Exact search remains a valid plan, and pgvector documents parallel
workers as one way to accelerate it without an ANN index.^9

This maps directly to YoDb's semantic plan alternatives:

```text
exact vector scan           high recall, increasing latency with candidate set
HNSW / IVFFlat shortlist    bounded latency, recall-sensitive
model verify every row      proposition-quality baseline, expensive
model verify shortlist      lower cost, quality depends on shortlist recall
```

### Filtered ANN is a central V0.1 concern

With approximate indexes, pgvector applies `WHERE` filtering after index scan,
so a restrictive filter can yield fewer results than requested. pgvector 0.8+
offers iterative scans that continue scanning until enough results are found or
configured bounds are reached; strict and relaxed ordering make explicit
recall/order trade-offs.^9

Therefore V0.1 must treat the following as plan parameters and report them:

```text
embedding model and distance metric
exact versus HNSW versus IVFFlat
shortlist target and oversampling factor
filter selectivity estimate
iterative-scan policy and max scan/probe budget
candidate recall measurement when labels exist
```

The planner must be free to choose an exact scan for a small filtered candidate
set. “An ANN index exists” is not sufficient justification to use it.

## 8. Graph execution principles

Graph queries are primarily sensitive to fan-out, path length, direction,
predicate placement, and result duplication. A bounded graph path is analogous
to a sequence of joins, but its intermediate result can grow exponentially with
branching factor. This is why bounded hops and traversal budgets are semantic
guardrails rather than implementation conveniences.

Neo4j uses a planner that relies on database information/statistics to produce
efficient plans, and its runtime executes the chosen plan.^10 ^11 PostgreSQL edge
tables use the relational planner and ordinary joins. The same logical traversal
can therefore have two physical implementations.

### V0.1 graph policy

```text
logical path:       typed declared relationships, directions, per-hop filters
physical choices:   Postgres joins | Neo4j Cypher adapter
required estimates: start cardinality, relationship degree, hop count, output cap
safety policy:      max_hops, max_frontier, max_edges, timeout, deduplicate by logical ID
```

Choose Postgres edge tables by default when the relevant data and hydration are
already in Postgres and path depth/fan-out is small. Consider Neo4j only when a
binding exists and measured graph traversal cost justifies the remote round trip
and data transfer. Do not infer that “graph” means Neo4j.

## 9. Transactions, isolation, and read consistency

### What a transaction guarantees locally

ACID means atomicity, consistency, isolation, and durability for one database
transaction. PostgreSQL uses MVCC: each statement sees a database snapshot, and
reads generally do not conflict with writes; it offers `READ COMMITTED`,
`REPEATABLE READ`, and serializable isolation machinery.^12 ^13 Neo4j likewise
provides ACID transactions, write-ahead logging, and read-committed isolation by
default; its clusters use bookmarks for causal consistency/read-your-writes
ordering.^14 ^15

### What V0.1 cannot promise

A Postgres read and Neo4j read are separate transactions with separate clocks,
replication paths, and failure domains. V0.1 has no distributed transaction,
global snapshot, or atomic cross-source read. It must not imply otherwise.

The V0.1 result contract should expose:

```text
source name and binding version
source read timestamp / transaction snapshot information when available
graph bookmark or source version token when available
freshness warning and partial-source failure
source precedence used for conflicting logical fields
```

For a read-only V0.1, do not use two-phase commit or distributed locking. They
solve atomic distributed writes, add failure/recovery complexity, and provide no
benefit to a query layer that does not mutate sources. If later read-your-writes
semantics are needed for Neo4j, propagate a bookmark through that source's
adapter only; do not mistake it for a global Postgres+Neo4j snapshot.^15

### Future write mode

The existing in-memory canonical-store concepts—optimistic versions,
tombstones, idempotency, outbox events, and derived-index reconciliation—are
appropriate for a future YoDb-owned canonical-write profile. They should remain
separate from V0.1's source-read semantics.

## 10. Failure, retries, and cancellation

Database systems expect failures as normal control flow: serialization failures,
deadlocks, timeouts, connection loss, overload, and stale statistics. PostgreSQL
detects deadlocks and aborts one participant, so callers must make retry policy
explicit rather than assume a lock acquisition always succeeds.^16

V0.1 should classify failures by operator and source:

```text
retryable source failure      connection reset, transient timeout, overload
non-retryable query failure   invalid binding, unsupported operator, bad type
budget failure                candidate/byte/model/traversal cap exceeded
partial result                optional source failed after policy allows degradation
consistency warning           source read versions differ or binding is stale
```

Retries must be bounded, cancellation must cascade to active source/model calls,
and an `EXPLAIN AI` record must distinguish “not selected,” “failed,” and
“partially executed.” Never retry a model verification blindly when it has a
side effect; V0.1 model calls should be side-effect-free.

## 11. Explainability is a database feature, not a debug afterthought

`EXPLAIN` works because it shows the plan tree and estimates; `EXPLAIN ANALYZE`
adds actual rows and timing, enabling estimation error to be observed.^1 YoDb
should reproduce that pattern across databases and models.

```text
EXPLAIN AI

Logical plan
  Scan tickets
  Filter plan = enterprise
  SemanticFilter proposition P
  Traverse relationship R

Candidate plans
  A: Postgres filter -> verify 43,210 rows
  B: Postgres filter -> pgvector 500 -> verify 500 rows
  C: Postgres filter -> Neo4j path -> verify 1,100 rows

Selected plan
  B because quality estimate >= threshold and lowest feasible cost

Actual execution
  source timings, rows, transferred bytes, token usage, cost, warnings
```

An explain record should include the logical query fingerprint, catalog/binding
versions, selected source capabilities, every rejected plan and rejection
reason, estimates, actuals, and a redacted representation of model inputs. This
is indispensable for user trust and for improving estimates.

## 12. A practical V0.1 planner design

Avoid an open-ended optimizer framework at first. Implement a pipeline with
explicit extension seams:

```text
parse structured IR
  -> validate schemas, bindings, access scope, and budgets
  -> normalize filters/projections/path constraints
  -> enumerate a small finite set of physical templates
  -> estimate rows, bytes, tokens, latency, cost, quality, freshness
  -> discard infeasible templates
  -> choose according to the documented policy
  -> execute with per-operator budgets/cancellation
  -> collect actuals and emit ExplainRecord
```

Suggested physical templates initially:

| Logical pattern | Templates |
| --- | --- |
| Relational filter/project | Postgres parameterized SQL |
| SemanticFilter | Postgres filter -> verify all; Postgres filter -> pgvector -> verify shortlist |
| Traverse | Postgres edge joins; Neo4j traversal |
| Semantic + graph | filter -> semantic shortlist -> traverse; filter -> traverse -> semantic verify |
| Cross-source hydration | source IDs -> declared join binding -> target source fetch |

Each adapter declares only what it can prove it supports:

```text
filter operators, projection, ordering, limit, cursor behavior
semantic candidate search and available distance metric
graph relationship/path support
parameter limits, max result size, timeout behavior
freshness/version token availability
```

This capability declaration is not a universal database interface. It is a
contract for safe pushdown and explainability.

## 13. What to implement now, later, and never emulate

| Principle / mechanism | V0.1 priority | YoDb action |
| --- | --- | --- |
| Logical/physical separation | Must | Typed IR and physical-plan objects |
| Filter/projection/limit pushdown | Must | Adapter capability checks and SQL/Cypher compilation |
| Cardinality, width, fan-out estimates | Must | Catalog statistics plus actual telemetry |
| Bounded candidate/data movement | Must | Per-operator caps and structured budget errors |
| Source-local transactions | Must | Read-only transaction/timeout policy per adapter |
| Distributed transactions | Do not build | Report independent source reads/freshness instead |
| Cost-based join enumeration | Later | Start with fixed templates and explicit heuristics |
| Cascades-style memo optimizer | Later | Preserve IR/plan separation so it can be added |
| ANN recall controls | Must | Exact/ANN alternatives, iterative-scan and shortlist policy |
| Adaptive query re-optimization | Later | Compare estimates to actuals first |
| Automatic index/materialization management | Later | Collect workload evidence and recommend only |
| Graph recursion / arbitrary patterns | Do not build in V0.1 | Explicit bounded paths and frontier budgets |
| Global snapshot across sources | Do not promise | Source provenance and partial/freshness status |

## 14. Design conclusions

1. **The planner should optimize work elimination before backend selection.**
   Push deterministic filters and projections down, limit fan-out, and reduce
   model candidates before comparing Postgres and Neo4j alternatives.

2. **The planner's principal V0.1 state is statistics plus budgets.** Without
   cardinality, candidate, transfer, token, and graph-degree estimates, routing
   is only hard-coded control flow with a planner label.

3. **Semantic planning is a quality-constrained top-k problem.** The vector
   shortlist is a recall-sensitive access path, not evidence that the semantic
   proposition is true. Preserve Plan A as an evaluation baseline.

4. **Federation makes data movement a first-class cost.** A remote graph call
   that returns millions of node IDs is a bad plan even if Cypher executes
   quickly. Require declared joins and bound all transfers.

5. **Consistency must be reported, not fictionalized.** V0.1 can provide
   source-local read consistency and provenance, but not a single atomic view
   across Postgres and Neo4j.

6. **Explainability is the control plane for trust.** The explain record should
   make every source choice, model call, quality assumption, budget decision,
   and source-read warning inspectable.

7. **Preserve the future canonical-write model separately.** It solves a
   legitimate later problem—source-of-truth writes and derived-state lifecycle—
   but conflating it with read-only federation would make V0.1 misleading.

## Sources

1. PostgreSQL Global Development Group. “[Using EXPLAIN](https://www.postgresql.org/docs/17/using-explain.html).” PostgreSQL 17 Documentation, accessed September 2026.
2. P. Griffiths Selinger et al. “[Access Path Selection in a Relational Database Management System](https://research.ibm.com/publications/access-path-selection-in-a-relational-database-management-system).” IBM Research / SIGMOD, 1979.
3. Goetz Graefe and William J. McKenna. “[The Volcano Optimizer Generator: Extensibility and Efficient Search](https://15721.courses.cs.cmu.edu/spring2023/papers/16-optimizer1/graefe-icde1993.pdf).” ICDE, 1993.
4. Goetz Graefe. “[The Cascades Framework for Query Optimization](https://liuyehcf.github.io/resources/paper/The-Cascades-Framework-For-Query-Optimization.pdf).” IEEE Data Engineering Bulletin, 1995.
5. PostgreSQL Global Development Group. “[How the Planner Uses Statistics](https://www.postgresql.org/docs/17/planner-stats-details.html).” PostgreSQL 17 Documentation, accessed September 2026.
6. Donald Kossmann and Michael J. Franklin. “[A Study of Query Execution Strategies for Client-Server Database Systems](https://drum.lib.umd.edu/items/5caf343d-6cf3-4ed0-8817-d43a3e152fe2).” University of Maryland Technical Report, 1998.
7. PostgreSQL Global Development Group. “[Writing a Foreign Data Wrapper](https://www.postgresql.org/docs/17/fdwhandler.html).” PostgreSQL 17 Documentation, accessed September 2026.
8. Amol Deshpande and Joseph M. Hellerstein. “[Decoupled Query Optimization for Federated Database Systems](https://www2.eecs.berkeley.edu/Pubs/TechRpts/2001/5664.html).” UC Berkeley Technical Report, 2001.
9. pgvector contributors. “[pgvector README](https://github.com/pgvector/pgvector/blob/master/README.md?plain=1).” Accessed September 2026.
10. Neo4j. “[Execution Plans](https://neo4j.com/docs/cypher-manual/4.4/execution-plans/).” Cypher Manual, accessed September 2026.
11. Neo4j. “[Cypher Runtimes](https://neo4j.com/docs/cypher-manual/current/planning-and-tuning/runtimes/).” Cypher Manual, accessed September 2026.
12. PostgreSQL Global Development Group. “[Introduction to Concurrency Control](https://www.postgresql.org/docs/17/mvcc-intro.html).” PostgreSQL 17 Documentation, accessed September 2026.
13. PostgreSQL Global Development Group. “[Concurrency Control](https://www.postgresql.org/docs/15/mvcc.html).” PostgreSQL Documentation, accessed September 2026.
14. Neo4j. “[Database Internals and Transactional Behavior](https://neo4j.com/docs/operations-manual/current/database-internals/).” Operations Manual, accessed September 2026.
15. Neo4j. “[Coordinate Transactions and Enforce Causal Consistency](https://neo4j.com/docs/query-api/current/bookmarks/).” Query API Manual, accessed September 2026.
16. PostgreSQL Global Development Group. “[Explicit Locking](https://www.postgresql.org/docs/17/explicit-locking.html).” PostgreSQL 17 Documentation, accessed September 2026.
