# Phase 0 — Logical Data Semantics

This document records the behavioral contracts that YoDb must establish before
adding persistence, physical backends, or a public API. Decisions here are
implemented through domain-model code and executable tests.

## Decision status

| Topic | Status |
| --- | --- |
| Record validation and unknown fields | Decided (initial contract) |
| Logical ID format and generation | Decided |
| Write, delete, and version semantics | Decided |
| Canonical persistence guarantee | Decided |
| Index freshness and eventual consistency | Decided |
| Query semantics | Decided (initial contract) |
| Relationship behavior | Decided (initial contract) |
| Error model | Decided |
| Schema evolution | Decided (V0 constraints) |

## Record validation

A `DatasetSpec` is the authority for a record's declared fields. On every
write, YoDb will validate a record against that dataset's schema and either
produce a normalized canonical record or reject the write with field-level
validation errors.

Initial validation rules:

- Declared values must conform to their `FieldSpec` type.
- A repeated field must receive a list-like value and each member must satisfy
  the declared field type.
- `id` is a system-owned logical identifier. Its exact format and generation
  policy are a separate pending decision.
- Unknown fields are rejected by default.
- Validation errors must identify the affected field, a machine-readable code,
  and a human-readable explanation.

### Field presence, nulls, and defaults

Each field has independent `required`, `nullable`, and `default` properties:

```python
FieldSpec(
    type=FieldType.STRING,
    required=True,
    nullable=False,
    default=None,
)
```

- If an input field is missing and `required=True`, reject the write.
- If an input field is missing and a default is declared, use that default.
- If an input field is explicitly `null`, accept it only when `nullable=True`.
- A default applies only to a missing field; it never replaces an explicit
  `null`.
- A field cannot be both `required=True` and have a default, because those
  instructions conflict.
- A `null` default is valid only for a nullable field.
- V0 defaults are literal values only. Dynamic defaults such as a current
  timestamp or generated ID are deferred.

### Missing versus explicit null

An optional field with no default is omitted from the canonical record when it
is not supplied. YoDb does not convert omitted input fields into `null`.

```text
absent field  -> omitted from `fields`
explicit null -> present in `fields` with value null; valid only if nullable
default value -> present in `fields` with the materialized default
```

This distinction preserves whether a value was never supplied versus explicitly
cleared. It also prevents sparse records from being padded with unnecessary
null values.

### Type parsing and coercion

YoDb will parse standard serialized forms where JSON has no equivalent native
type, but will not silently guess an application's intent. A type mismatch is a
validation error unless it is explicitly accepted below.

| Declared type | Accepted input | Normalized internal value | Rejected examples |
| --- | --- | --- | --- |
| `string`, `text` | string | string | numbers, booleans, objects |
| `int` | integer, excluding boolean | integer | `"42"`, `42.0`, `true` |
| `float` | integer or float, excluding boolean | float | `"3.14"`, `true` |
| `bool` | boolean | boolean | `"yes"`, `0`, `1` |
| `timestamp` | ISO-8601 string with timezone; aware datetime internally | UTC-aware datetime | timezone-less strings |
| `uuid` | canonical UUID string; UUID object internally | UUID | arbitrary string |
| `id` | non-empty logical ID string | string | number, blank string |
| `json` | JSON-compatible value | JSON-compatible value | custom Python object |
| `bytes` | base64 string at an API boundary; bytes internally | bytes | arbitrary text |

Repeated fields accept a list at a JSON/API boundary. Each member is parsed and
validated using the scalar rule for that field's declared type.

## Unknown fields

### Policy

Datasets will expose an explicit opt-in flag:

```python
DatasetSpec(
    name="documents",
    allow_unknown_fields=False,  # default
    # ...
)
```

When `allow_unknown_fields` is `False`, a write with a field that is not
declared by the dataset fails validation. This is the default because it
catches misspellings, prevents accidental schema drift, and preserves the
meaning of a typed logical dataset.

### Opt-in behavior

When `allow_unknown_fields` is `True`, undeclared values are accepted only if
they are JSON-compatible. They are kept apart from schema-governed data:

```text
CanonicalRecord
├── id
├── fields     declared, typed dataset fields
└── extra      accepted undeclared JSON-compatible values
```

Values in `extra` are not filterable, sortable, searchable, or indexable by
default. A field must be formally added to the dataset schema before it can
participate in those capabilities.

### Reserved names

Canonical metadata is represented outside the user `fields` map. Within a
record-create or record-update payload, only `id` is reserved because YoDb owns
logical identity:

```text
id
```

System metadata such as `version`, `created_at`, `updated_at`, and `deleted_at`
exists in the canonical record envelope, not at the same namespace as declared
fields. A dataset may therefore legitimately declare a field named
`created_at` or `updated_at`.

## Acceptance examples

Given a strict dataset:

```python
DatasetSpec(
    name="documents",
    allow_unknown_fields=False,
    fields={"id": FieldSpec(FieldType.ID), "title": FieldSpec(FieldType.STRING)},
)
```

This input is rejected:

```json
{ "title": "Roadmap", "titlte": "misspelled field" }
```

Given the same dataset with `allow_unknown_fields=True`, this input is valid
and normalizes conceptually to:

```json
{
  "fields": { "title": "Roadmap" },
  "extra": { "source_specific_field": "value" }
}
```

An input supplying an undeclared `version` field is rejected even when unknown
fields are enabled, because `version` is reserved.

## Logical IDs

YoDb owns logical record identity. A record ID must never be a PostgreSQL row
ID, a Qdrant point ID, a Neo4j node ID, or any other physical-backend identity.

### Format

YoDb-generated record IDs have this form:

```text
ydb_<ULID>
```

The `ydb_` prefix makes the identifier recognizable in logs and APIs. The ULID
component is globally unique and lexicographically time-sortable. IDs are
opaque to applications: consumers must not parse the timestamp or infer any
storage placement from them.

### Generation and ownership

- On a normal create without an `id`, YoDb generates the ID.
- A V0 create request that supplies an `id` is rejected; clients cannot choose
  a YoDb logical ID.
- Read, update, and delete operations identify records using the YoDb ID that
  was returned on creation.
- An ID is immutable and is never reused, including after deletion.
- An external/source-system identifier belongs in a separately declared field,
  such as `external_id` or `source_id`; it is not a substitute for YoDb's
  logical ID.

## Write, version, and deletion semantics

V0 provides explicit create, partial update, and logical delete operations.
It deliberately does not provide generic replacement or upsert behavior.

| Operation | V0 behavior |
| --- | --- |
| `create` | Creates a new record with a YoDb-generated ID and initial version `1`. |
| `update` | Applies an explicit partial change to an existing record. |
| `replace` | Not supported; replacing an entire record can accidentally erase fields. |
| `upsert` | Not supported; it is ambiguous until YoDb has declared unique external-key semantics. |
| `delete` | Creates a logical-deletion tombstone. |
| `restore` | Not supported in V0; it will require explicit retention and authorization policy. |

### Optimistic concurrency

Every update and delete requires the record version the caller expects to
change. A successful canonical modification increments the version by one.

```text
current record version: 4

update(id, expected_version=4, set={...}) -> succeeds; new version: 5
update(id, expected_version=4, set={...}) -> version conflict
```

This prevents concurrent clients, services, or agents from silently overwriting
one another's changes.

### Logical deletion

Deleting a record does not immediately destroy its canonical history. Instead,
YoDb creates a tombstone and increments the record version.

- Normal reads and queries treat a logically deleted record as nonexistent.
- The tombstone contains enough identity and change information for derived
  indexes to remove their copies reliably.
- Deleted IDs are never reused.
- Access to deleted records and eventual physical purging are deferred to the
  retention and authorization designs.

## Canonical persistence guarantee

In V0, PostgreSQL is YoDb's sole canonical store. A canonical record contains
the YoDb ID, version, declared fields, accepted `extra` values, and
logical-deletion state. Canonical relationships are likewise stored as edge
records in PostgreSQL.

All other representations are derived and rebuildable:

- pgvector embeddings and vector indexes;
- lexical/search indexes;
- relationship traversal indexes;
- future copies in Qdrant, Neo4j, ClickHouse, or any other backend.

### Authoritative-write and read contract

- YoDb acknowledges a create, update, or delete only after its canonical
  PostgreSQL transaction commits.
- A read by YoDb record ID retrieves canonical PostgreSQL data.
- A search index may provide candidate IDs, but YoDb treats the canonical
  record and deletion state as authoritative before returning results.
- If a derived representation disagrees with canonical PostgreSQL data,
  PostgreSQL wins and the representation must be reconciled or rebuilt.
- If a derived representation is unavailable, canonical data remains safe;
  only the affected derived capability may be degraded or unavailable.
- If canonical PostgreSQL is unavailable, V0 rejects writes. It never accepts
  a write solely in a derived index or secondary backend.

Canonical authority does not yet promise multi-region disaster recovery,
infinite retention, or a particular backup/replica strategy. Those operational
guarantees belong to production hardening.

## Index freshness and bounded staleness

Canonical writes are strong. Derived vector, lexical, and future graph indexes
are eventually consistent, but their lag is observable and bounded by policy.
Every canonical mutation atomically records a durable index-change event in the
same PostgreSQL transaction. An index projector processes those events
idempotently.

Each derived index exposes at least:

```text
state:              ready | lagging | failed | rebuilding
applied_sequence:   last canonical change processed by this index
canonical_sequence: latest canonical change requiring processing
lag_changes:        canonical_sequence - applied_sequence
lag_age:            age of the oldest unapplied change
last_updated_at
```

### Write backpressure

Every V0 derived index is write-gating: it has a maximum permitted lag in both
change count and elapsed time, plus a write-backpressure timeout. These values
may become dataset requirements/configuration in a later phase.

```text
max_lag_changes
max_lag_age
write_backpressure_timeout
```

Before accepting a new canonical write, YoDb checks every required derived
index. If any one exceeds either lag threshold, YoDb pauses admission of the
new write and waits for the index to recover below its threshold.

- YoDb must not hold an open PostgreSQL write transaction while waiting.
- If the index recovers before `write_backpressure_timeout`, YoDb performs the
  canonical write normally.
- If it does not recover, YoDb rejects the write with a structured
  `index_backpressure_timeout` error and makes no canonical change.
- Once a write has committed canonically, it remains committed even if an index
  becomes unhealthy afterwards; later writes are the ones subject to
  backpressure.

This provides bounded eventual consistency rather than pretending that an
asynchronous index is strongly consistent.

### Query freshness and safety

The default derived-index query mode is `eventual`; responses report index
freshness metadata. A caller that needs a known mutation to be searchable can
request `require_indexed_through=<sequence>`, causing YoDb to wait or return a
freshness-timeout error.

A stale derived index is never allowed to resurrect a logically deleted record.
YoDb validates canonical deletion state before returning index candidates.
Indexes that fail can be retried or rebuilt from canonical PostgreSQL data and
the durable change stream.

## Query semantics

### Backend-neutral filter language

YoDb filters are structured logical expressions, never raw SQL, Cypher, or
backend-specific query syntax. The expression tree is part of YoDb's public
logical query model and is translated below the abstraction boundary.

```json
{
  "all": [
    { "field": "status", "eq": "published" },
    { "field": "created_at", "gte": "2026-01-01T00:00:00Z" },
    {
      "any": [
        { "field": "author_id", "eq": "ydb_01..." },
        { "field": "is_public", "eq": true }
      ]
    }
  ]
}
```

V0 filter operators:

| Field kind | Operators |
| --- | --- |
| All comparable scalar fields | `eq`, `ne`, `in`, `not_in`, `exists` |
| `int`, `float`, `timestamp` | `gt`, `gte`, `lt`, `lte` |
| `string`, `text` | `contains`, `starts_with` |
| Repeated fields | `contains`, `overlaps` |
| Boolean composition | `all`, `any`, `not` |

Rules:

- A predicate may reference only a declared field marked `filterable=True`.
- Predicate values use the same parsing and type rules as record writes.
- Unknown fields and unsupported operator/type combinations are validation
  errors.
- An empty filter means all non-deleted records in the dataset.
- Normal logical queries automatically exclude deleted records; callers cannot
  bypass deletion state through a normal filter.

Projection, ordering, pagination, semantic/lexical search, and relationship
traversal composition remain to be decided.

### Projection

A query returns a stable record envelope:

```json
{
  "id": "ydb_01K...",
  "version": 4,
  "fields": {
    "title": "Roadmap",
    "created_at": "2026-09-12T18:00:00Z"
  }
}
```

By default, YoDb returns all declared fields together with the system-owned
`id` and `version`. It does not return `extra` by default, because `extra` is
schema-less overflow data and may later require distinct permission controls.

Callers can select declared fields explicitly:

```json
{ "select": ["title", "created_at"] }
```

`id` and `version` remain present in the envelope; only the selected declared
values appear inside `fields`.

- `select` may contain only declared dataset fields.
- Unknown and duplicate selected fields are validation errors.
- A non-selected field is omitted rather than represented as `null`.
- `extra` is inaccessible through normal V0 projection. A future explicit
  `include_extra` option requires authorization design.

### Ordering

```json
{
  "order_by": [
    { "field": "created_at", "direction": "desc" },
    { "field": "title", "direction": "asc" }
  ]
}
```

- Ordering may reference only declared fields marked `sortable=True`.
- `id` is always sortable as a system field.
- If no ordering is supplied, the order is `id ASC`.
- YoDb appends `id ASC` as a final tie-breaker to every requested order. This
  makes ordering deterministic and enables safe cursor pagination.
- `null` sorts last in both ascending and descending order in V0.
- Repeated fields cannot be sorted.
- Future semantic-search queries may expose a result-only `score` ordering;
  `score` is not a record field.

### Pagination

V0 uses cursor (keyset) pagination exclusively. Offset pagination is not
supported because it becomes inefficient for large datasets and shifts
unpredictably as records are inserted or deleted.

```json
{ "limit": 50, "after": "opaque_cursor_here" }
```

| Concern | V0 policy |
| --- | --- |
| Default page size | 100 records |
| Maximum page size | 500 records |
| Cursor contents | Dataset, schema version, query/order fingerprint, final sort tuple, final ID, issue time |
| Cursor representation | Opaque and signed; callers must not parse or construct it |
| Cursor lifetime | 15 minutes by default |
| Final page | `next_cursor` is `null` |
| Query change | Fails with `cursor_query_mismatch` |
| Incompatible schema change | Fails with `cursor_invalidated` |

The cursor remembers the last result's effective order tuple, including the
always-appended `id ASC` tie-breaker. The following page requests records
strictly after that tuple.

Normal pagination is not a database snapshot. Concurrent inserts may not appear
in an active traversal, deletions disappear from later pages, and changing a
record's sort value can cause it to shift, be skipped, or appear twice. A future
frozen export/audit capability must use an explicit `as_of` snapshot or
read-version contract instead.

### Semantic and lexical retrieval

Search requests express retrieval intent, not a physical execution choice. A
caller never supplies an index name, backend, collection, embedding model, or
physical algorithm.

```json
{
  "dataset": "documents",
  "semantic_search": {
    "query": "how should a data platform evolve?",
    "freshness": { "mode": "eventual" }
  },
  "where": { "field": "status", "eq": "published" },
  "select": ["title", "content", "created_at"],
  "limit": 20
}
```

Lexical retrieval has the parallel shape `lexical_search: {"query": "..."}`.
The dataset schema/capabilities identify the logical fields eligible for each
mode; physical indexes are planner and catalog concerns.

| Topic | V0 policy |
| --- | --- |
| Search modes | A query contains zero or one retrieval mode: `semantic_search` or `lexical_search`. |
| Hybrid retrieval | Deferred. It must be an explicit future operation with declared fusion/ranking semantics, never a silent mixture. |
| Filters | Apply to final returned records. The planner may push them into a physical retrieval operation when supported. |
| Ranking | Default order is `score DESC, id ASC`. V0 does not permit custom field ordering alongside retrieval. |
| Score | Result metadata, never a record field. It is comparable only within the same query execution. |
| Empty query text | Validation error. |
| Candidate count | The public `limit` is final-result count; any oversampling is an internal planner choice. |

Search produces candidate IDs. YoDb then validates canonical existence and
deletion state and returns canonical projected fields. A stale derived index
cannot resurrect a logically deleted record. Search responses expose logical
freshness state but not the physical index/backend selected by the planner.

### Planner-owned physical search strategy

The planner first chooses a suitable available implementation, such as a
materialized pgvector index. If no vector index exists, it may choose a bounded
exact scan over stored per-record embeddings. Vector similarity requires
embeddings; it cannot be computed by scanning raw text alone. If no embedding
representation exists, the planner may derive/cache it only within configured
work and latency budgets.

An unbounded scan is never an implicit fallback. If no available plan can meet
the query's safety budget, YoDb returns a structured capability/budget error.

Future telemetry records query frequency, scan cost, candidate count, and
latency. Under an explicit policy and resource budget, repeated expensive
searches can cause YoDb to recommend or provision a materialized index. This
automatic physical optimization is not required in V0, but the public request
shape and planner boundary must already permit it.

### Search pagination

Semantic and lexical retrieval use a short-lived search session rather than a
stateless keyset cursor. On the first search request, YoDb executes retrieval,
records a bounded ordered candidate set, and returns an opaque search cursor.
Later pages continue through that original candidate set.

| Concern | V0 policy |
| --- | --- |
| Result order | Fixed as `score DESC, id ASC` for a session |
| Session/cursor lifetime | 15 minutes |
| Candidate-set bound | Retain at most the top 2,000 candidates |
| Index changes after page one | Do not change the session's candidate order |
| Record changes after page one | Hydrate current canonical fields; skip records now logically deleted |
| Query change | Fails when a cursor is reused with different retrieval text, filters, or mode |
| Expiry | Fails with `search_cursor_expired`; caller starts a new search |

The session internally captures the selected physical plan and index watermark,
but it never exposes an index/backend choice to the caller. Search-session
pagination stabilizes relevance order; it is not a frozen snapshot of record
contents.

### Relationship traversal

V0 supports bounded multi-hop traversal through explicitly declared
relationships. A path contains one or more hops up to the configured
`max_traversal_hops` policy (initial V0 default: three). Each hop names a
`RelationshipSpec` and a direction, never SQL join syntax, Cypher, a table, or
a graph backend.

```json
{
  "from": {
    "dataset": "documents",
    "where": { "field": "status", "eq": "published" }
  },
  "path": [
    { "relationship": "authored_by", "direction": "out" },
    { "relationship": "member_of", "direction": "out" },
    {
      "relationship": "belongs_to",
      "direction": "out",
      "target_where": { "field": "name", "eq": "Membrane" }
    }
  ],
  "return": "source"
}
```

The example returns published documents whose author belongs to Membrane.

- A path may contain no more than the configured `max_traversal_hops`; its
  initial V0 default is three.
- Every hop must match a declared relationship, its direction, and the dataset
  type produced by the previous hop.
- Each hop may use typed filters on declared, filterable edge fields and target
  fields.
- A traversal may return the source records or final target records; returned
  records are deduplicated by YoDb ID by default.
- Logically deleted edges and endpoint records are excluded.
- YoDb applies a configured traversal-work budget to intermediate edge/node
  expansion and returns `traversal_budget_exceeded` when it is exceeded.
- Arbitrary-length paths, wildcard relationships, recursive patterns, and
  cycles are deferred.

V0 executes these paths using canonical PostgreSQL edge tables and bounded SQL
joins. Apache AGE or another graph implementation may later become a
planner-selected physical alternative, but is not required by the public query
contract or V0 deployment.

Relationship write behavior—edge identity, duplicate-edge policy, cardinality
enforcement, and endpoint-deletion behavior is defined below.

### Relationship writes and lifecycle

Every relationship edge has its own immutable YoDb-generated ID and version.
It connects two live canonical records in the source and target datasets named
by its `RelationshipSpec`.

- Edge source and target are immutable. Changing either requires logically
  deleting the old edge and creating a new edge.
- Declared edge fields support partial updates with `expected_version` and
  optimistic concurrency, just like record fields.
- Edge deletion is logical: it creates a tombstone and increments edge version.
- By default, a duplicate active `(relationship, source_id, target_id)` edge is
  rejected.
- Future event-like relationship types may opt into parallel edges explicitly;
  their exact schema flag is deferred until relationship specs are extended.

### Cardinality enforcement

Cardinality is enforced atomically in the canonical PostgreSQL transaction when
an edge is created or changed. Active edge cardinality means:

| Cardinality | Constraint |
| --- | --- |
| `one_to_one` | Each source has at most one outgoing edge and each target at most one incoming edge. |
| `one_to_many` | A source may have many outgoing edges; each target has at most one incoming edge. |
| `many_to_one` | Each source has at most one outgoing edge; a target may have many incoming edges. |
| `many_to_many` | Sources and targets may each have many edges. |

Concurrent attempts that violate a constraint fail with a structured cardinality
error; two clients cannot each successfully create the sole permitted edge.

### Endpoint deletion

Logically deleting a canonical record logically deletes all of its active
incident edges in the same canonical operation. This ensures a deleted endpoint
cannot retain live graph connections and leaves durable edge tombstones for
derived graph representations to reconcile.

## Error model

YoDb uses one transport-independent public error contract across its Python
domain layer and future HTTP, CLI, and agent interfaces.

```json
{
  "error": {
    "code": "record_validation_failed",
    "message": "Record is invalid for dataset 'documents'.",
    "retryable": false,
    "request_id": "req_01K...",
    "details": {
      "violations": [
        {
          "path": "fields.created_at",
          "code": "invalid_timestamp",
          "message": "Expected an ISO-8601 timestamp with a timezone."
        }
      ]
    }
  }
}
```

- `code` is stable, machine-readable, and snake_case. Clients branch on it.
- `message` is human-readable and may improve without becoming an API contract.
- `retryable` indicates whether an unchanged retry might work.
- `request_id` links a public response to private logs/traces.
- `details` has a documented shape for its error family.
- Raw Python, PostgreSQL, index-provider, and backend errors never cross the
  public boundary.

| Family | Codes |
| --- | --- |
| Invalid input | `record_validation_failed`, `query_validation_failed`, `invalid_request` |
| Missing logical object | `dataset_not_found`, `relationship_not_found`, `record_not_found`, `edge_not_found` |
| Conflict | `version_conflict`, `cardinality_violation`, `duplicate_edge` |
| Cursor/session | `cursor_query_mismatch`, `cursor_invalidated`, `search_cursor_expired` |
| Freshness/backpressure | `index_backpressure_timeout`, `index_freshness_timeout` |
| Query safety | `traversal_budget_exceeded`, `query_budget_exceeded`, `capability_unavailable` |
| Availability | `canonical_store_unavailable`, `index_unavailable` |
| Unexpected failure | `internal_error` |

`version_conflict` includes expected and actual versions. An
`index_backpressure_timeout` includes logical affected capability, observed lag,
and an optional `retry_after_ms`. A traversal/query budget error includes the
configured and observed work.

Normal callers receive `record_not_found` for logically deleted records, not
`record_deleted`, so deletion state does not leak. Future administrative/audit
access may define distinct authorization and visibility behavior.

## Schema evolution

A dataset and relationship schema is versioned and immutable once active. A
change creates a new version; YoDb does not mutate the meaning of existing data
in place. The catalog retains prior versions while exactly one version is active
for normal writes.

Each canonical record and edge records the schema version under which it was
written. Schema updates require `expected_schema_version`, preventing concurrent
administrators from silently overwriting one another's changes. Cursors include
a schema version and are invalidated by an incompatible schema activation.

### Logical capabilities versus physical indexes

`filterable`, `sortable`, and `searchable` are logical field capabilities. They
express what a caller is allowed to request; they do not require or reveal a
materialized physical index.

The planner may satisfy a permitted operation with an existing index, a
PostgreSQL scan, or another safe physical implementation. It may later create
or recommend an index because workload telemetry justifies it. Absence of an
index alone must never make a declared logical field capability unavailable.

### V0 schema changes

V0 supports safe additive changes without an explicit migration engine:

| Change | V0 behavior |
| --- | --- |
| Add an optional nullable field | Create and activate a new schema version; existing records omit it. |
| Add an optional edge field | Create and activate a new schema version; existing edges omit it. |
| Add a relationship | Create and activate a new schema version. |
| Mark a field filterable, sortable, or searchable | Create and activate a new schema version; the planner chooses a safe implementation without requiring an index build. |
| Add a derived index | Treated as an optional physical optimization, not a prerequisite for the corresponding logical capability. |

Defaults apply to future writes. Applying a newly introduced default to existing
canonical records is a historical backfill and is not supported in V0.

V0 rejects breaking changes rather than attempting an implicit conversion:

- rename or remove a field;
- change a field type;
- make an optional field required;
- change relationship endpoints or meaning;
- tighten relationship cardinality;
- apply a new default retroactively.

Future versions will provide an explicit, resumable migration workflow:

```text
planned -> backfilling -> validating -> building_indexes -> ready -> active
                                                       -> failed
```

That workflow will use canonical backfills, derived-index rebuilds, validation,
and an atomic activation step. It is deliberately not part of V0.
