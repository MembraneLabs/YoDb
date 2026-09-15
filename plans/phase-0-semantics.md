# Phase 0 — Logical Data Semantics

This document records the behavioral contracts that YoDb must establish before
adding persistence, physical backends, or a public API. Decisions here are
implemented through domain-model code and executable tests.

> **V0.1 authority notice.** The initial parts of this document describe a
> future YoDb-managed canonical-write mode. V0.1 instead is a read-only,
> source-authoritative federated query layer. The V0.1 decisions and binding
> contract below control whenever they conflict with the earlier write,
> generated-ID, canonical-persistence, tombstone, or derived-index sections.
> The earlier material remains a future-mode reference until it is fully
> reorganized.

## Decision status

| Topic | Status |
| --- | --- |
| Record validation and unknown fields | Decided (initial contract) |
| Logical ID format and generation | Decided |
| Write, delete, and version semantics | Future YoDb-managed write mode; not V0.1 |
| Canonical persistence guarantee | Future YoDb-managed write mode; not V0.1 |
| Index freshness and eventual consistency | Future YoDb-managed write mode; not V0.1 |
| Query semantics | Partially retained; V0.1 source bindings and result contract control |
| Relationship behavior | Logical traversal retained; relationship writes are future mode |
| Error model | V0.1 contract decided below |
| Schema evolution | V0.1 catalog-only rules; no source migration |
| V0.1 source authority and read boundary | Decided |
| V0.1 catalog and binding configuration | Decided |
| V0.1 normalized result envelope | Decided |

## V0.1 source authority and catalog bindings

### Source-authoritative read boundary

V0.1 does not own source data. It is read-only and does not create a shadow
canonical record store. A connected PostgreSQL or Neo4j source remains
authoritative for its own data, identity, write transaction, deletion, version,
and physical-index behavior.

YoDb owns the logical catalog, query IR, validation, planning, result
normalization, logical result identity, provenance, and explainability.

- V0.1 provides no create, update, delete, write routing, distributed
  transaction, or global cross-source snapshot guarantee.
- Each source operation uses that source's ordinary read semantics.
- A response reports source read timestamps/freshness and any allowed partial
  failure; it must not imply a single global snapshot.
- When multiple sources provide the same field, catalog metadata
  declares source precedence. The preferred value is returned and a material
  disagreement is surfaced in explain/debug output rather than silently merged.
- A missing or deleted record in the preferred hydration source cannot be
  resurrected from a graph or secondary representation.

### Responsibility boundary

```text
YoDb:          inspect source schema/capabilities; validate bindings;
               plan, execute, normalize, and explain reads.

Developer or
coding agent:  author every DatasetSpec, SourceBinding, FieldBinding,
               RelationshipBinding, JoinBinding, and LogicalIdBinding.

Human owner:   supplies or approves business meaning and activates a catalog
               version.
```

V0.1 deliberately does **not** infer, suggest, auto-generate, or activate a
dataset, field mapping, relationship, cross-source match, or join.
Schema inspection exposes factual physical metadata only. A coding agent may
use that metadata and user-provided business context to write a complete catalog
configuration, but YoDb only validates that configuration; it never invents
business meaning or an executable mapping.

### Catalog configuration contract

The executable, user-authored V0.1 YAML contract is defined in
[v0.1-yaml-catalog-schema.md](v0.1-yaml-catalog-schema.md). It supersedes the
older illustrative configuration shape in this section where they differ. The
YAML deliberately has only `datasets.yaml`, `sources.yaml`, and `relations.yaml`;
it does not expose named mapping objects. The terms below remain useful
conceptually and inside Python, but not as a required user-facing YAML layer.

The active catalog is an immutable, versioned declarative configuration. The
runtime representation may be Pydantic/Python objects, while the portable
configuration representation is YAML or JSON. Every physical reference must be
explicit; no name-based fallback is permitted. The catalog is **closed-world**:
an unmapped physical column or property is inaccessible for every YoDb purpose,
including filtering, projection, ordering, semantic retrieval, identity, and
join execution.

The initial configuration has this shape:

```text
CatalogSpec
├── version
├── sources: SourceSpec[]
├── datasets: DatasetSpec[]
└── relationships: RelationshipBinding[]
```

#### `SourceSpec`

A `SourceSpec` registers one connected physical data source.

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | Immutable catalog-local source identifier. It is not a hostname. |
| `kind` | Yes | `postgres` or `neo4j` in V0.1. |
| `connection_ref` | Yes | Reference to externally managed credentials/configuration; credentials are never embedded in catalog files. |
| `read_only` | Yes | Must be `true` in V0.1. |
| `enabled` | No | Whether the source may be selected by the planner; defaults to `true`. |

#### `DatasetSpec`

A dataset is a stable business-level entity such as `Customer`,
`SupportTicket`, or `Order`. It is not necessarily a physical table.

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | Immutable dataset name. |
| `description` | Yes | Human-readable business meaning. |
| `aliases` | No | Business synonyms used by SDK/MCP catalog discovery. |
| `fields` | Yes | Declared logical `FieldSpec`s. |
| `source_bindings` | Yes | One or more explicit physical representations. |
| `identity_binding` | Yes | The source binding that supplies the stable logical identity. |
| `field_precedence` | No | Preferred source binding per overlapping field. |

Logical fields retain their typed properties and add V0.1 metadata where
applicable: `description`, `aliases`, safe `example_values` or an enumeration,
`unit`, `sensitivity`, and `semantic_eligible`. Field capabilities describe
logical/access policy, never physical-index presence.

#### `SourceBinding` and `FieldBinding`

A source binding maps one dataset to one physical representation. A field
binding maps a declared field to one physical column or property.

| `SourceBinding` field | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | Immutable binding identifier within the dataset. |
| `source` | Yes | A `SourceSpec.name`. |
| `kind` | Yes | `postgres_relation` or `neo4j_label` in V0.1. |
| `relation` / `label` | Yes | Exact PostgreSQL schema-qualified table/view or Neo4j label. |
| `identity` | Yes | Ordered fields whose `FieldBinding` values form the immutable source key for this representation. |
| `fields` | Yes | Mapping of field names to `FieldBinding`s. |
| `read_timestamp_field` | No | Source field used as a freshness/version hint when one exists. |

| `FieldBinding` field | Required | Meaning |
| --- | --- | --- |
| `field` | Yes | Declared field name. |
| `column` / `property` | Yes | Exact physical column or Neo4j property name. |
| `source_type` | Yes | Inspected physical type, retained for validation and diagnostics. |
| `normalization` | No | Explicit source-to-logical conversion rule; omitted only when the mapping is directly compatible. |
| `visibility` | No | `public` (default) or `internal`. Internal fields are unavailable to caller projection/filter/order/search but may be used by declared identity or join bindings. |

Every identity and join key therefore has a `FieldBinding`, even when it is an
internal implementation key. YoDb never names an unmapped column/property in a
physical plan. Mapping a field as `internal` does not expose it to an
application, agent, MCP client, or normal result envelope; it only makes the
approved physical value available to the planner for the declared purpose.

#### `LogicalIdBinding`

A logical result ID is deterministic, source-derived, and opaque. It is never a
physical database row identifier exposed as a universal ID.

```text
logical ID = stable encoding of
  (dataset, immutable identity-binding name, canonical source-key tuple)
```

`DatasetSpec.identity_binding` selects the authoritative identity binding. The
binding's ordered `identity` field bindings must resolve to non-null, unique
source values for that representation. Other representations of the same
dataset must connect to this identity through an explicit approved
`JoinBinding`; matching table names, columns, emails, values, or apparent IDs
is not identity evidence.

#### `RelationshipBinding` and `JoinBinding`

A relationship binding expresses the business meaning of an edge. A join
binding expresses exactly how YoDb can resolve that edge physically.

| `RelationshipBinding` field | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | Immutable logical relationship name. |
| `source_dataset` | Yes | Logical origin dataset. |
| `target_dataset` | Yes | Logical target dataset. |
| `description` | Yes | Business meaning of the relationship. |
| `aliases` | No | Business synonyms for catalog discovery. |
| `cardinality` | Yes | `one_to_one`, `one_to_many`, `many_to_one`, or `many_to_many`. |
| `join_bindings` | Yes | One or more approved physical resolution paths. |

| `JoinBinding` field | Required | Meaning |
| --- | --- | --- |
| `name` | Yes | Immutable identifier for this physical resolution path. |
| `kind` | Yes | `equality`, `bridge_table`, or `graph_edge` in V0.1. |
| `source_binding` | Yes | Exact source-side dataset binding. |
| `target_binding` | Yes | Exact target-side dataset binding. |
| `steps` | Yes | Ordered explicit equality joins or graph-edge endpoints, each referring only to declared field bindings. |
| `source_of_truth` | Yes | Binding used for preferred final hydration when this path returns overlapping values. |

An `equality` binding has one equality step. A `bridge_table` binding names
every bridge relation and equality step. A `graph_edge` binding names its graph
source, exact labels, relationship type, endpoint field bindings, and direction.
A join step names a field plus a source binding; the planner resolves
the physical column/property only through that field's `FieldBinding`. A query
can use only a declared relationship and one of its valid join bindings; it
cannot issue arbitrary SQL/Cypher, refer to an unmapped physical field, or infer
a cross-source link.

### Illustrative declarative configuration

This is an example of a complete, human/agent-authored binding. It is
illustrative of the contract; exact parser syntax follows the Python model.

```yaml
version: 1

sources:
  - name: crm_postgres
    kind: postgres
    connection_ref: secret://yodb/crm-readonly
    read_only: true

  - name: support_postgres
    kind: postgres
    connection_ref: secret://yodb/support-readonly
    read_only: true

datasets:
  - name: Customer
    description: A company with a commercial account.
    aliases: [client, account, organization]
    identity_binding: crm_customer
    fields:
      - name: id
        type: id
        description: Stable customer identity.
      - name: name
        type: string
        description: Customer company name.
      - name: plan_tier
        type: string
        description: Current commercial subscription level.
        aliases: [plan, subscription tier]
        example_values: [enterprise, business, starter]
    source_bindings:
      - name: crm_customer
        source: crm_postgres
        kind: postgres_relation
        relation: public.accounts
        identity: [id]
        fields:
          - field: id
            column: account_uuid
            source_type: uuid
          - field: name
            column: company_name
            source_type: text
          - field: plan_tier
            column: subscription_level
            source_type: text

  - name: SupportTicket
    description: A customer support case and its conversation.
    identity_binding: support_ticket
    fields:
      - name: id
        type: id
        description: Stable ticket identity.
      - name: customer_id
        type: id
        description: Customer identity recorded by the support system.
      - name: content
        type: text
        description: Customer support conversation and issue description.
        semantic_eligible: true
    source_bindings:
      - name: support_ticket
        source: support_postgres
        kind: postgres_relation
        relation: public.tickets
        identity: [id]
        fields:
          - field: id
            column: ticket_id
            source_type: uuid
          - field: customer_id
            column: crm_account_uuid
            source_type: uuid
            visibility: internal
          - field: content
            column: body
            source_type: text

relationships:
  - name: Customer.has_ticket
    source_dataset: Customer
    target_dataset: SupportTicket
    description: A support ticket associated with a customer.
    cardinality: one_to_many
    join_bindings:
      - name: crm_customer_to_support_ticket
        kind: equality
        source_binding: crm_customer
        target_binding: support_ticket
        source_of_truth: crm_customer
        steps:
          - left: { binding: crm_customer, field: id }
            right: { binding: support_ticket, field: customer_id }
```

### Catalog validation and activation

YoDb validates a submitted catalog version without modifying source data. It
checks that every named source, relation/label, column/property, field,
identity key, and join endpoint exists; that types and explicit normalization
rules are compatible; and that identity/relationship cardinality claims are
consistent with inspected constraints or optional read-only profiling.

The user may use a coding agent to author the entire configuration from raw
schema inspection and business context. Before activation, YoDb exposes
validation/profiling facts such as key uniqueness, null rates, join coverage,
and observed duplicate counts. It does not turn those facts into a suggested
mapping or activate a mapping automatically.

### Existing in-memory domain layer: V0.1 boundary

The current in-memory record/edge implementation was designed for a future
YoDb-managed canonical-write mode. It is retained as useful domain groundwork,
but it is not a V0.1 storage engine, read cache, or source of record truth.

| Existing concept | V0.1 treatment |
| --- | --- |
| `FieldType`, `FieldSpec` | Retain and extend for logical catalog and binding metadata. |
| `DatasetSpec` | Retain and evolve into the dataset catalog definition. |
| `RelationshipSpec` | Retain and evolve into logical relationship semantics. |
| Structured errors and type validation | Retain; extend with catalog, connector, planner, consistency, and budget error families. |
| YoDb-generated `ydb_<ULID>` record IDs | Do not use for V0.1 externally owned source records. |
| In-memory record/edge store | Do not use as a V0.1 data store, canonical cache, or fallback read source. |
| Create/update/delete, versions, tombstones | Deferred to future YoDb-managed write mode. |
| Edge creation and write-time cardinality enforcement | Deferred; V0.1 reads declared relationships only. |

V0.1 connector results are authoritative for record contents at their reported
source read time. Local V0.1 state may contain catalog metadata, short-lived
cursor/search-session state, telemetry, and bounded explicitly non-authoritative
caches. It must never silently serve a locally copied source record as a
canonical or fresher value.

Consequently, existing write-domain tests remain valuable future-mode
regressions, but they do not prove V0.1 behavior. V0.1 needs separate tests for
catalog loading, bindings, logical-ID derivation, query validation, compilation,
normalization/provenance, pagination, budgets, and source-failure behavior.

### V0.1 normalized result envelope

YoDb normalizes source reads into a stable logical response. A source row/node
is never returned in raw physical shape, and YoDb does not attach a V0.1-owned
record version, tombstone state, or `extra` field bag.

```text
QueryResponse
├── query_id
├── results: LogicalRecord[]
├── page
├── sources
└── warnings

LogicalRecord
├── id
├── dataset
├── fields
├── provenance
└── match                 optional operation-specific metadata
```

| Response field | Required | V0.1 meaning |
| --- | --- | --- |
| `query_id` | Yes for accepted queries | Stable identifier for explain output, telemetry, and support diagnostics. |
| `results` | Yes | Ordered normalized logical records. |
| `page.next_cursor` | No | Opaque continuation cursor/session token; `null` on a final page. |
| `sources` | Yes | Safe summary of participating logical source/binding names and their read timestamps/freshness. |
| `warnings` | Yes | Structured non-fatal conditions; an empty array means no known degradation. |

| `LogicalRecord` field | Required | V0.1 meaning |
| --- | --- | --- |
| `id` | Yes | Opaque deterministic logical ID derived through `LogicalIdBinding`. |
| `dataset` | Yes | Logical dataset name. |
| `fields` | Yes | Requested, public, mapped fields only. |
| `provenance` | Yes | Identity binding and source read timestamp; authorized explain/debug output may add field-source details. |
| `match` | No | Query-result metadata for semantic verification or traversal; never a persisted record field. |

Illustrative relational response:

```json
{
  "query_id": "qry_01...",
  "results": [
    {
      "id": "ydb_...",
      "dataset": "Customer",
      "fields": {
        "name": "Acme",
        "plan_tier": "enterprise"
      },
      "provenance": {
        "identity_binding": "crm_customer",
        "read_at": "2026-09-14T10:00:00Z"
      }
    }
  ],
  "page": { "next_cursor": null },
  "sources": [
    { "binding": "crm_customer", "read_at": "2026-09-14T10:00:00Z" }
  ],
  "warnings": []
}
```

Field-state rules are explicit:

- A selected mapped field with a source `null` is returned as `null`.
- A non-selected field is omitted from `fields`.
- An `internal` field is always omitted from normal results.
- YoDb must never fabricate `null` for a value it could not read. A required
  source failure fails the request; an explicitly allowed partial response
  omits unavailable fields/results and names the exact impact in `warnings`.
- Sensitive physical source keys, credentials, hostnames, raw SQL/Cypher, and
  raw source exceptions are not normal result/provenance data.

Semantic and graph metadata stay outside `fields`. For example, a semantic
result may include a verification decision/confidence and model version, while a
traversal result may include a bounded path summary. Detailed graph evidence is
opt-in, authorized, and budgeted.

### V0.1 error and warning contract

YoDb exposes one transport-neutral public error envelope across the Python SDK,
future HTTP interfaces, and MCP. Public errors are stable, machine-readable,
and safe: raw PostgreSQL/Neo4j/model-provider exceptions, SQL/Cypher,
credentials, and stack traces never cross the boundary.

```json
{
  "error": {
    "code": "query_budget_exceeded",
    "message": "The requested query exceeds its configured traversal-work budget.",
    "retryable": false,
    "request_id": "req_01...",
    "query_id": "qry_01...",
    "details": {
      "budget": "max_intermediate_nodes",
      "configured": 10000,
      "observed": 12543
    }
  }
}
```

- `request_id` is present for every request, including requests rejected before
  query planning.
- `query_id` is present once YoDb has accepted a valid query for planning or
  execution; it may be absent for malformed requests.
- `code` is stable snake_case and is the machine contract. `message` is
  human-readable and may improve without breaking clients.
- `retryable=true` means the identical request may plausibly succeed later.
  Invalid input, invalid bindings, and exhausted fixed budgets are not
  retryable; a transient read timeout usually is.
- `details` has a documented, family-specific safe shape.

| Error family | V0.1 codes |
| --- | --- |
| Catalog/configuration | `catalog_validation_failed`, `source_binding_invalid`, `field_binding_invalid`, `join_binding_invalid`, `logical_id_binding_invalid` |
| Query validation | `query_validation_failed`, `dataset_not_found`, `field_not_found`, `field_not_accessible`, `relationship_not_found` |
| Cursor/session | `cursor_query_mismatch`, `cursor_invalidated`, `search_cursor_expired` |
| Capability/planning | `capability_unavailable`, `plan_constraint_unsatisfied` |
| Budget/safety | `query_budget_exceeded`, `traversal_budget_exceeded`, `candidate_transfer_budget_exceeded` |
| Source/read availability | `source_unavailable`, `source_timeout`, `source_read_failed` |
| Source compatibility | `source_value_incompatible`, `source_schema_drift_detected` |
| Unexpected failure | `internal_error` |

A required source failure fails the request. A partial result is allowed only
when the caller explicitly sets `allow_partial_results=true` and the planner
can identify the impact. It is represented as a successful `QueryResponse`
with a structured warning, never as an unexplained omission:

```json
{
  "code": "partial_source_unavailable",
  "source": "relationship_graph",
  "skipped_operation": "bounded_traversal",
  "impact": "Results exclude graph-derived constraints."
}
```

Conflicting values from sources are normally warnings rather than errors. The
catalog-preferred source value is returned, and authorized explain/debug output
identifies the conflicting binding/value state. This policy prevents silent
merges without turning an otherwise usable read into a failure.

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
meaning of a typed dataset.

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
The dataset schema/capabilities identify the fields eligible for each
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

`filterable`, `sortable`, and `searchable` are field capabilities. They
express what a caller is allowed to request; they do not require or reveal a
materialized physical index.

The planner may satisfy a permitted operation with an existing index, a
PostgreSQL scan, or another safe physical implementation. It may later create
or recommend an index because workload telemetry justifies it. Absence of an
index alone must never make a declared field capability unavailable.

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
