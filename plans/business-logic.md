# Business logic in YoDb: design

**Status:** proposal. Nothing here is built. It is written against `main` at commit `83a94d1`
(version 0.1.1).

**What this is:** a design for how YoDb can hold a company's business rules, apply them to every query,
and let an AI agent use them by name without being able to change or bypass them.

**Companion:** [business-logic-build-path.md](business-logic-build-path.md) gives the order to build it in,
one small stage at a time. This file says *what* and *why*; that file says *in what order and how*.

**How to read it:** sections 1-3 set out the problem and the principles. Section 4 lists every kind of
business logic and what to do with each. Sections 5-8 are the design: how rules are written, how a query
is rewritten, how an agent uses them, and how people write and maintain them. Sections 9-12 cover risks,
what not to build, how to know it works, and the decisions that need a human.

---

## 1. The problem

YoDb today removes one kind of guessing. An agent no longer guesses table names, column names or join
keys, because a person declared them once in the catalog.

It does not remove the other kind. The agent still decides what the question means:

- "Active customers": is that `status = 'active'`, or also "ordered in the last 90 days"?
- "UK": is the stored value `UK` or `GB`?
- "Open tickets": should tickets from deleted customers and test accounts count?
- "Revenue": gross or net, with or without refunds?

A wrong choice still returns rows, and they look right. This is the same failure as a wrong join key, one
level up. The company knows the answer to each of these. It is just not written anywhere YoDb can use.

**Goal:** a company writes its rules down once, next to its data description. After that, every query
gets the same meaning for the same word, whoever or whatever asks.

---

## 2. Principles

These decide every design choice below. Where two options exist, the one that fits these wins.

1. **Queries carry names, not logic.** YoDb already works this way for joins: the agent picks a
   relationship by name and cannot invent one. Rules work the same way. If a query could contain a
   free-form expression, the agent would be writing business logic again.
2. **Formalise what fails silently.** If a wrong guess gives an error, a description is enough. If a wrong
   guess gives plausible wrong rows, it needs a named, checked definition.
3. **Rules are data, not code.** They are declarative and typed, with a closed set of constructs. That is
   what lets them be validated, shown, compared between versions and tested. No scripts, no functions
   supplied by users.
4. **Meaning and location stay separate.** `datasets.yaml` says what things mean. `sources.yaml` says
   where they are. Rules about meaning must not name tables; anything physical goes in `sources.yaml`.
5. **Rewrite before planning.** A rule is turned into the filters and fields the engine already
   understands, before the planner sees the query. Then pushdown, row limits, `explain` and the test
   oracles all keep working unchanged. The engine only changes where it lacks a building block.
6. **Nothing is applied invisibly.** Every rule that touched a query is listed in `explain` and in the
   result. A person can always answer "why is this row missing?"
7. **Fail closed.** An unknown name is an error. A rule that cannot be applied is an error. YoDb never
   skips a rule to get an answer out.
8. **Same question, same answer.** A query, a catalog and a moment in time give one result. "Now" is fixed
   once per query and reported.
9. **Two levels of trust.** People who write the catalog are trusted. Callers are not. Anything that
   identifies the caller (tenant, role) comes from the program hosting YoDb, never from the query.
10. **Rules must be maintainable by the people who own them.** That means readable files, clear errors,
    tests that live with the rules, and a way to see what a change will do before it ships.

---

## 3. What YoDb has today

| Piece | State | Where |
| --- | --- | --- |
| Datasets, fields, types | Done | `catalog.py`, `datasets.yaml` |
| Which records are the same across databases (identity) | Done | `sources.yaml` `identity`, `resolution` |
| How datasets connect (relationships) | Done | `relations.yaml` |
| Hidden fields | Done: `visibility: internal` | `FieldSpec` |
| Descriptions, `aliases`, `example_values` | Present, advisory only | `FieldSpec`, `DatasetSpec` |
| A way to add a new kind of filter term | Done: the semantic filter uses it | `query/extensions.py` |
| Catalog and query fingerprints | Done | `query/fingerprint.py` |
| Named filters, always-on rules, time, derived fields, metrics | Nothing | |

Four facts about the current code shape this design:

- **Binding refuses internal fields.** `bind_public_field` in `query/validation.py` is the only path, and
  it rejects `visibility: internal`. Always-on rules need to filter on hidden fields, so they need a
  second, trusted path that callers cannot reach.
- **A join plans each side as its own query.** `joins/planner.py` builds a sub-query per side. Rules must
  therefore be applied per dataset, on each side, not once per query.
- **The catalog is strict.** Every model forbids unknown keys. New keys are a format change that old
  versions of YoDb will refuse, which is the safe behaviour.
- **`describe_catalog` returns everything at once.** That is fine for two datasets. With rules and
  metrics added it will not fit an agent's context, so discovery has to become searchable.

Three existing problems would get worse with rules, so the build path fixes them first:

- **A text operator blocks pushdown of its neighbours.** Seen in this repository: with `starts_with` in an
  `all`, `country` was no longer pushed to the database. A named condition containing `contains` would
  silently turn narrow reads into full reads. To be confirmed and fixed before conditions ship.
- **An alias cannot be used in a query.** `{"dataset": "client"}` is refused although `client` is a
  declared alias.
- **A catalog must declare at least one relationship.** An empty `relations.yaml` does not load.

---

## 4. Every kind of business logic

This is the full map. Each row says what the logic is, how to write it down, and who applies it. "Engine"
means YoDb enforces it on every query. "Agent" means the agent reads it and is trusted to use it well.

### 4.1 What words and values mean

| Kind | Example | Representation | Applied by | Priority |
| --- | --- | --- | --- | --- |
| Synonyms | "client" means `customer` | `aliases`, made to resolve or to suggest | Engine | High |
| Closed value sets | country is `UK`, never `GB` | `allowed_values` on a field | Engine: a value outside the set is refused | High |
| Stored codes | status `1` means "open" | `codes` on the field's mapping in `sources.yaml`: logical value to stored value | Engine: translates filters and results | Medium |
| Value groups and hierarchies | EMEA is these 40 countries | `groups` on a field | Engine: a group name is accepted wherever a list is | Medium |
| Units and currency | `amount` is in cents, USD | `unit` on a field | Agent reads it; engine shows it | High (cheap) |
| What "no value" means | empty `plan` means "no subscription" | `null_means` on a field | Agent reads it | High (cheap) |
| Sentinel values | `-1` or `1970-01-01` means unknown | `sentinels` on a field | Engine: treats them as no value | Low |
| Caveats | "unreliable before 2023" | `caveats` on a field or dataset | Agent reads it; returned with results | High (cheap) |
| Glossary terms | "churn" | a glossary entry pointing at a definition | Agent finds it by search | Medium |

### 4.2 Which rows count

| Kind | Example | Representation | Applied by | Priority |
| --- | --- | --- | --- | --- |
| Named conditions | "paying customer", "overdue invoice" | `conditions` on a dataset: a name and a filter | Engine expands it | High |
| Conditions with a parameter | "inactive for N days" | a condition with typed `parameters` | Engine | Medium |
| Always-on rules | never return deleted rows or test accounts | `always` on a dataset | Engine, on every query, not overridable | High |
| Current version of a row | only the row where `valid_to` is empty | an `always` rule | Engine | High (same mechanism) |
| Tenant or region scoping | a caller sees only its own tenant | an `always` rule using a context value | Engine | Medium |
| Conditions across datasets | "customers with an open ticket" | a condition with a `has` clause | Engine; needs a new operator | Low (later) |
| Lifecycle and SLA states | "breached: open more than 48 hours at priority 5" | a named condition using relative time | Engine | Covered by the above |

### 4.3 Time

| Kind | Example | Representation | Applied by | Priority |
| --- | --- | --- | --- | --- |
| Relative time | "in the last 30 days" | a relative value: `{"relative": "-30d"}` | Engine | High |
| Named periods | "this month", "last quarter" | a period value: `{"period": "last_quarter"}` | Engine | Medium |
| Time zone for day boundaries | "today" in London | `time_zone` in the catalog | Engine | Medium |
| Fiscal calendar | fiscal year starts in April | `fiscal_year_start` in the catalog | Engine | Medium |
| Business days and holidays | "within 2 working days" | needs a calendar table | Not planned | Low |

### 4.4 Computed values

| Kind | Example | Representation | Applied by | Priority |
| --- | --- | --- | --- | --- |
| Derived fields from one table | `is_overdue`, `full_name`, `days_open` | an `expression` in `sources.yaml` in place of a column | The database | Medium |
| Buckets and tiers | "enterprise" if seats > 500 | the same, with a `CASE` expression | The database | Medium |
| Which source wins | use billing's email, else CRM's | an ordered list in `field_sources` | Engine, when merging rows | Medium |
| Derived fields across databases | a value from two servers | a small portable expression language | Engine | Low (later) |

### 4.5 Numbers

| Kind | Example | Representation | Applied by | Priority |
| --- | --- | --- | --- | --- |
| Counts and totals | "how many open tickets" | a named `metric` | Engine; needs aggregation | High value, high cost |
| Filtered measures | "revenue" is the sum of paid amounts | a metric with a named condition | Engine | Same |
| Ratios | "churn rate" | a metric built from two metrics | Engine | Later |
| Breakdowns | "by country, by month" | `dimensions` and time grains allowed for a metric | Engine | Same |

### 4.6 How things connect

| Kind | Example | Representation | Applied by | Priority |
| --- | --- | --- | --- | --- |
| The usual path | which of two relationships is "the" one | `default: true` on a relationship | Agent reads it | Low (cheap) |
| Roles | billing address or shipping address | two relationships with clear names and descriptions | Agent reads it | Already possible |
| Repeated rows | one customer, many tickets | from `cardinality`: a note on the result | Engine adds the note | Medium (cheap) |

### 4.7 Who may see what

| Kind | Example | Representation | Applied by | Priority |
| --- | --- | --- | --- | --- |
| Hidden fields | internal keys | `visibility: internal` | Engine | Done |
| Fields by role | only finance sees `salary` | `visible_to` on a field | Engine | Medium |
| Masking | show the last four digits | `mask` on a field | Engine, on results | Medium |
| Row access | a caller sees its own region | an `always` rule with a context value | Engine | Medium |

### 4.8 How a dataset may be queried

| Kind | Example | Representation | Applied by | Priority |
| --- | --- | --- | --- | --- |
| Required filters | events must have a date range | `require` on a dataset | Engine: refuses otherwise | Medium |
| Default order | "top customers" means by lifetime value | `default_order` on a dataset | Engine | Low |
| Deprecation | use `net_amount`, not `amount` | `deprecated` with a replacement | Engine warns; later refuses | Medium (cheap) |
| Freshness | this table is loaded nightly | `freshness` on a source dataset | Engine reports it with results | Low |

### 4.9 Out of scope

- **Rules about writing data**: validation, allowed status changes, workflows. YoDb is read-only.
- **Formatting for display**: rounding, date formats, currency symbols. That belongs to whatever shows the
  answer.
- **Rules that need judgement**: "is this customer at risk?" If a model must decide, that is the semantic
  filter, not a rule.

---

## 5. How rules are written

### 5.1 Where they live

Rules about one field go on that field in `datasets.yaml`, because they are part of its definition.
Rules about rows, time and metrics go in a new, optional fourth file, `rules.yaml`.

```text
catalog/
├── datasets.yaml    what exists (now also: allowed values, units, caveats)
├── sources.yaml     where it lives (now also: expressions, stored codes, which source wins)
├── relations.yaml   how datasets connect
├── rules.yaml       what words mean: conditions, always-on rules, time, metrics   (new, optional)
└── checks.yaml      examples with known answers                                    (new, optional)
```

Why a separate file: rules change far more often than the schema, and often belong to different people.
A separate file keeps their history readable and lets a team own it. This is decision D1 in section 12.

A catalog without `rules.yaml` behaves exactly as today.

### 5.2 Field meaning, in `datasets.yaml`

```yaml
datasets:
  customer:
    description: A customer of the shop.
    aliases: [client, account]
    fields:
      country:
        type: string
        description: Where the customer is billed.
        allowed_values: [US, UK, FI]
        groups:
          europe: [UK, FI]
      plan:
        type: string
        description: Billing plan.
        allowed_values: [free, pro, team]
        null_means: The customer has no subscription.
      balance:
        type: int
        description: Amount owed.
        unit: USD cents
        caveats: [Not reliable for accounts created before 2023.]
```

- `allowed_values` closes the set. A filter on `country = "GB"` is refused and the error lists the allowed
  values. Today it returns no rows and no error.
- `groups` names a set of values. `{"field": "country", "op": "in", "value": {"group": "europe"}}` uses it.
- `unit`, `null_means` and `caveats` are text for the reader. They are shown in discovery, and caveats are
  also returned with any result that used the field.

### 5.3 Named conditions, in `rules.yaml`

```yaml
api_version: yodb/v0.1
rules:
  customer:
    conditions:
      paying:
        description: On a paid plan.
        where: {field: plan, op: in, value: [pro, team]}
      new:
        description: Signed up within the last N days.
        parameters:
          days: {type: int, default: 30, minimum: 1, maximum: 365}
        where: {field: signed_up, op: gte, value: {relative: "-{days}d"}}
      new_paying:
        description: A paying customer who signed up recently.
        where: {all: [{condition: paying}, {condition: new}]}
```

A query uses a condition wherever a filter term can go:

```json
{"from": {"dataset": "customer"},
 "where": {"all": [
   {"condition": "paying"},
   {"condition": "new", "with": {"days": 7}},
   {"field": "country", "op": "eq", "value": "UK"}
 ]}}
```

Rules for conditions:

- A condition's `where` uses the same filter language as a query. There is nothing new to learn.
- A condition may use other conditions of the same dataset. Loops are refused when the catalog loads.
- A condition may sit under `all`, `any` or `not`, because it expands to ordinary filters.
- Parameters are typed and bounded. A value outside the bounds is refused.
- In a join, a condition in the top-level `where` belongs to the starting dataset. A condition inside a
  `traverse` step belongs to the traversed dataset.

### 5.4 Always-on rules, in `rules.yaml`

```yaml
rules:
  customer:
    always:
      - name: not_deleted
        description: Deleted customers are never returned.
        where: {field: deleted_at, op: is_null}
        visible: false
      - name: no_test_accounts
        description: Internal test accounts are excluded.
        where: {field: is_test, op: eq, value: false}
```

- Every always-on rule of a dataset is added to every query on it, combined with AND.
- It applies to the dataset on either side of a join.
- It may filter on a field with `visibility: internal`. Callers cannot see that field or filter on it
  themselves.
- A caller cannot switch it off. There is no query key that does so.
- `visible: false` hides the rule's content from agents. They are told that a rule applied, by name and
  description, but not which field it used.

Always-on rules never contradict each other in a way that needs resolving. They only ever narrow the
result, and they are combined with AND. There is no "override" and no ordering between them.

### 5.5 Values from the caller's context

```yaml
rules:
  ticket:
    always:
      - name: own_tenant
        description: A caller sees only its own tenant's tickets.
        where: {field: tenant_id, op: eq, value: {context: tenant}}
```

The value of `tenant` comes from the program that opened YoDb:

```python
db = yodb.connect("catalog/", context={"tenant": "acme"})
```

- A context value can never be set by a query or through an MCP tool argument.
- If a rule needs a context value that was not supplied, the query is refused. It is never run without
  the rule.

### 5.6 Time, in `rules.yaml`

```yaml
time:
  zone: Europe/London
  fiscal_year_start: 4          # April
```

Two new kinds of value are accepted wherever a timestamp is expected, in queries and in rules:

| Value | Meaning |
| --- | --- |
| `{"relative": "-30d"}` | 30 days before now. Units: `h`, `d`, `w`, `mo`, `y` |
| `{"period": "this_month"}` | A named period. With `gte` it means its start; with `lt`, its end |

"Now" is read once when the query starts, used for every relative value in that query, and returned with
the result. Two reads of the clock inside one query could otherwise give a result that is true at no
single moment.

### 5.7 Derived fields, in `sources.yaml`

The field is declared in `datasets.yaml` like any other. Its mapping in `sources.yaml` is an expression in
place of a column:

```yaml
# datasets.yaml
is_overdue: {type: bool, description: Past its due date and not paid.}

# sources.yaml
fields:
  is_overdue: {expression: "due_date < now() AND status <> 'paid'"}
```

- The database computes it, so filtering and sorting on it are pushed down like any column.
- `yodb validate` checks the expression against the real table and checks that its type matches.
- It can only use columns of its own table. That limit is deliberate: it avoids building an expression
  language.
- The catalog author writes it. A caller never supplies SQL, so the "no SQL in queries" rule is intact.

A field can also name more than one source, in order, when two databases hold the same fact:

```yaml
resolution:
  customer:
    field_sources: {email: [billing, crm]}     # billing's value, or CRM's when billing has none
```

### 5.8 Metrics, in `rules.yaml`

```yaml
rules:
  ticket:
    metrics:
      open_count:
        description: Number of open tickets.
        aggregate: count
        where: {condition: open}
        dimensions: [priority, status]
      average_priority:
        description: Mean priority of tickets.
        aggregate: avg
        field: priority
```

A metric is asked for with its own query shape, not through `select`:

```json
{"metric": {"dataset": "ticket", "name": "open_count"}, "by": ["priority"],
 "where": {"field": "customer_id", "op": "eq", "value": "c07"}}
```

Three rules keep metrics safe:

- **Only named metrics.** A query cannot write its own aggregation.
- **A metric is computed on its own dataset.** It is never computed after a join, which is how totals get
  double-counted.
- **Only declared dimensions.** A breakdown by an arbitrary field is refused.

Metrics need aggregation in the engine, which does not exist. This is the one part of the design that is
a new engine feature and not a rewrite. It gets its own design document before any code.

### 5.9 Tests, in `checks.yaml`

```yaml
api_version: yodb/v0.1
checks:
  - name: paying customers in the sample
    query: {from: {dataset: customer}, where: {condition: paying}}
    expect: {ids: [c01, c02, c04, c05, c07, c08, c10]}
  - name: deleted customers never appear
    query: {from: {dataset: customer}, where: {field: id, op: eq, value: c99}}
    expect: {count: 0}
  - name: paying and free do not overlap
    disjoint: [paying, free]
    dataset: customer
```

`yodb check catalog/` runs them. Section 8 describes it.

---

## 6. How YoDb applies rules to a query

### 6.1 The steps

A query goes through these steps. The ones in bold are new.

```text
1. Parse the JSON
2. **Resolve names**            aliases to real names, or a "did you mean" error
3. Bind to the catalog          datasets, fields, types
4. **Expand conditions**        replace each {"condition": ...} with its filter, filling parameters
5. **Add always-on rules**      one set per dataset, on each side of a join
6. **Fix context and time**     fill {"context": ...}; read "now" once and fill {"relative": ...}
7. **Translate values**         groups to lists, logical values to stored codes
8. Validate                     types, operators, **allowed values**, **required filters**, limits
9. Fingerprint                  of the rewritten query
10. Find the sources, plan, compile, run     unchanged
11. **Translate results**       stored codes back to logical values; apply masks
12. **Attach what was applied** rule names, "now", caveats
```

Steps 4 to 7 turn a query that uses names into a query that uses only what the engine has today. From
step 8 on, the planner cannot tell whether a filter was typed by a caller or came from a rule.

### 6.2 Why expansion comes before planning

- **Pushdown still works.** An expanded condition is an ordinary filter, so the database applies it.
- **The planner's estimates still work.** They are made from the same statistics.
- **The row limits still apply.** A rule cannot cause an unbounded read.
- **The existing tests still mean something.** The SQL oracle compares the expanded query.

### 6.3 Where it goes in the code

| Step | Change |
| --- | --- |
| Catalog model | New models in `catalog.py`; `rules.yaml` and `checks.yaml` loaded by `load_catalog`. Rules become part of `Catalog`, so the catalog fingerprint covers them with no extra work |
| Parse | `{"condition": ...}`, `{"relative": ...}`, `{"period": ...}`, `{"group": ...}` accepted by `query/parser.py` |
| Expand | A new module, `yodb/rules/`, called from `bind_query`. It returns ordinary bound filters |
| Trusted binding | A second binding path beside `bind_public_field`, used only for always-on rules, that accepts internal fields |
| Joins | `joins/planner.py` builds each side as a sub-query; expansion runs inside each, so no join code changes |
| Compile | `compilation/postgres.py` learns to write an expression where it writes a column today |
| Results | `QueryExecutionResult` gains `applied`; `reports` already exists for extensions |

Conditions are not added as an extension term like the semantic filter. An extension term stays in the
plan as its own operator. A condition should disappear into ordinary filters, so it is a rewrite, not an
operator.

### 6.4 Details that matter

- **Limits apply after expansion.** A filter may have 1,000 conditions and 32 levels. A small query that
  expands past that is refused. Expansion depth is capped separately.
- **A left join and an always-on rule.** The rule is applied to the traversed side before matching. A
  customer whose only tickets are deleted is kept, with empty ticket fields. That is the correct meaning
  of "deleted tickets do not exist".
- **Always-on rules and the semantic filter.** The rules are applied first, so the model judges fewer
  rows and costs less.
- **Fingerprints.** The query fingerprint is taken from the expanded query, so two queries with the same
  meaning share one. A relative time is fingerprinted as written (`-30d`), not as the moment it resolved
  to.
- **A rule that cannot be pushed down.** A condition that ORs fields from two databases cannot be sent to
  either, and on a large table it will hit the row limit. The catalog check warns about this when the
  rule is written, so it is not discovered in production.
- **Expressions have no statistics.** The planner falls back to its default estimate for a filter on a
  derived field. The plan stays correct and may be slower.
- **Stored codes and sorting.** Sorting on a field with `codes` sorts by the stored value. If the logical
  order differs, sorting on that field is refused in the first version. See decision D7.

### 6.5 New errors

All are additions, which the compatibility rules allow.

| Code | When |
| --- | --- |
| `condition_not_found` | No condition by that name on the dataset. The message suggests near matches |
| `condition_parameter_invalid` | A missing, unknown or out-of-range parameter |
| `value_not_allowed` | A filter value outside a field's `allowed_values`. The message lists them |
| `context_missing` | A rule needs a context value the host did not supply |
| `required_filter_missing` | The dataset requires a filter the query lacks |
| `metric_not_found`, `dimension_not_allowed` | For metrics |

---

## 7. How an agent uses rules safely

### 7.1 What an agent can and cannot do

| The agent can | The agent cannot |
| --- | --- |
| List and search definitions | Define or change one |
| Read a condition's meaning in logical terms | See a hidden rule's content, or any table or column name |
| Use a condition, group, period or metric by name | Write its own expression or aggregation |
| Pass typed parameters within their bounds | Switch off an always-on rule |
| See which rules were applied to its answer | Set a context value such as its tenant or role |

### 7.2 Discovery that scales

`describe_catalog` returns the whole catalog. With rules added it will outgrow an agent's context. Replace
one large answer with three smaller tools:

| Tool | Returns |
| --- | --- |
| `describe_catalog` | A summary only: each dataset's name, description and counts of what it has |
| `describe_dataset(name)` | One dataset in full: fields, allowed values, conditions, metrics, relationships |
| `find_definitions(text)` | Datasets, fields, conditions and metrics matching a word or phrase, with aliases and glossary terms searched |

An agent asked about "churned customers" calls `find_definitions("churn")` and gets the condition
`churned` with its description. It does not have to read every dataset to find it.

### 7.3 Steering the agent toward names

- The query guide the server gives the agent says: use a named condition when one exists; build a filter
  only when none fits.
- A refusal points to the right name. A filter on `plan in [pro, team]` works, but a filter on
  `country = "GB"` gets `value_not_allowed` with the allowed list.
- An unknown condition name gets near matches: "no condition `payng`; did you mean `paying`?"

### 7.4 What comes back with an answer

```json
{"rows": [...], "row_count": 6,
 "applied": {
   "conditions": [{"name": "paying", "dataset": "customer", "meaning": "plan in [pro, team]"}],
   "always": [{"name": "not_deleted", "dataset": "customer", "description": "Deleted customers are never returned."}],
   "now": "2026-10-10T09:00:00Z"
 },
 "notes": ["balance: Not reliable for accounts created before 2023."]}
```

This is what lets an agent say truthfully what it counted. It can tell the user "paying means on the pro
or team plan, and deleted customers are excluded", because YoDb told it so.

### 7.5 Trust boundaries

- **Descriptions are text, not instructions.** They are written by the catalog author and shown to the
  agent. The MCP page already says to treat rows and descriptions as untrusted if someone outside the
  team can write them. That stays true.
- **Expressions are trusted, and still checked.** They come from the catalog author. `yodb validate` runs
  each one in a read-only session with no rows returned, and refuses anything that is not a single
  expression.
- **Context is set by the host.** In the MCP server it comes from how the server was started, never from
  the conversation.
- **An audit record** of who asked, which rules applied and what was refused becomes possible once
  logging exists. Logging is part of the stabilisation work, not this plan.

---

## 8. How people write, inspect and maintain rules

Rules that are hard to write will not be written, and rules that cannot be checked will be wrong. This
section matters as much as the engine.

### 8.1 Writing

- **One language.** A condition's filter is the query filter language. Someone who can write a query can
  write a rule.
- **Errors that point at the line.** A catalog error names the file, the path (`rules.customer.conditions.paying.where`)
  and, where it can, the fix: "field `plans` does not exist on `customer`; did you mean `plan`?"
- **Editor support.** Publish a JSON Schema for each catalog file. Editors then offer completion and mark
  mistakes while typing. The stabilisation work already calls for these schemas.
- **A scaffold.** `yodb rules init catalog/` writes a `rules.yaml` with one commented example per dataset.

### 8.2 Inspecting

| Command | Shows |
| --- | --- |
| `yodb rules catalog/` | Every condition, always-on rule and metric, per dataset, with its description |
| `yodb rules catalog/ customer.paying` | One definition fully expanded, and which other definitions use it |
| `yodb explain catalog/ QUERY` | The plan, plus which rules were applied and what each expanded to |
| `yodb preview catalog/ customer --condition paying` | A few rows that match and a few that do not, from the real database |
| `yodb docs catalog/` | A glossary page for people: every term, its meaning, its owner |

`preview` is the fastest way for an author to see whether a rule says what they meant.

### 8.3 Checking

`yodb check catalog/` runs in two parts and exits non-zero on failure, so it can gate a merge.

**Without a database (lint):**
- every name used by a rule exists and has the right type;
- no condition refers to itself, directly or through others;
- a rule that cannot be pushed to a database is flagged, with the reason;
- an always-on rule uses a field that every source of the dataset can supply;
- definitions that nothing uses, missing descriptions, and aliases that clash are reported;
- anything deprecated that is still used is reported.

**With a database (tests from `checks.yaml`):**
- **examples:** a query and its expected IDs, count, or rows it must or must not contain;
- **relations between conditions:** two conditions never overlap; one implies another; a set of
  conditions covers every row.

### 8.4 Changing

- **See the effect first.** `yodb diff old-catalog/ new-catalog/` lists which definitions changed. For a
  changed condition it reports how many rows enter and leave it, within the row limits.
- **Replace all or nothing.** `db.refresh()` already keeps the old catalog if the new one fails to
  validate. Rules ride on that.
- **Deprecate before removing.** `deprecated: {replaced_by: net_amount, remove_after: 2027-01-01}`. A
  query that uses it works and gets a note. After the date, `yodb check` fails.
- **Say who owns it.** An optional `owner` on any definition, shown in `yodb rules` and `yodb docs`.
- **Version.** The catalog's `version` number and its fingerprint already identify what a query ran
  against. Both cover rules once rules are part of the catalog.

### 8.5 Not planned: a model that writes rules

A model could read a schema and propose conditions. It would be a drafting aid, and a person would still
have to review every line. It adds nothing to the engine, so it is left out here.

---

## 9. Risks

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| It grows into a full semantic layer | Cube and dbt already do metrics well, on one warehouse | Build row-level rules first. They need no new engine work, and no other tool applies them across databases. Treat metrics as a separate decision |
| An expression language creeps in | It is a large, permanent surface | SQL expressions in `sources.yaml` for one table. Nothing portable until a real need is shown |
| Rules make queries slow or make them fail | A rule that cannot be pushed reads whole tables | Fix the pushdown problem first; lint for unpushable rules; show cost in `explain` |
| Hidden rules confuse people | "Why is this customer missing?" | Every applied rule is named in the result and in `explain` |
| Rules go stale | A wrong definition is worse than none | Tests in `checks.yaml`, owners, deprecation, `yodb check` in CI |
| The agent ignores named conditions | It builds its own filter and gets a different meaning | Guide text, `allowed_values`, and making the names easy to find |
| The catalog outgrows the agent's context | Discovery stops working | Summary, per-dataset and search tools |
| A rule leaks what it hides | A hidden rule's field shows up in an error or a plan | Hidden rules are named but never expanded in anything a caller sees; tested |
| Too many small features | Each key is a promise once the format is stable | Every new key starts in the experimental tier of the compatibility page |

---

## 10. What not to build

- Expressions or aggregations written inside a query.
- User-supplied functions or scripts.
- Rules written as prose that a model enforces. They cannot be checked or repeated.
- A way for a caller to switch off an always-on rule.
- Rules about writing data.
- A general portable expression language, until section 4.4's one-table expressions prove insufficient.

---

## 11. How to know it works

**Coverage.** Collect 30 to 50 real business rules from one or two real companies. Sort each into
section 4. Count how many can be written with what is built. This is the main measure, and it should be
taken before building past stage 4 of the build path.

**Correctness.** For each construct, the generated-query test suite gains cases that use it, compared with
the same SQL oracle used today. A query that uses a condition must return exactly what the hand-expanded
query returns.

**Agent behaviour.** A fixed set of questions with known answers, run through an agent twice: with a
catalog that has only descriptions, and with one that has rules. Compare the share of correct answers and
of wrong answers given with confidence. If rules do not move those numbers, the design is wrong
somewhere.

**Cost.** Latency and rows read for the same questions, with and without rules. Rules should not make a
query read more.

**Authoring.** Time for a person who knows the data, and has not seen YoDb, to write and test five rules.

---

## 12. Decisions that need a human

| | Decision | Options | Recommendation |
| --- | --- | --- | --- |
| D1 | Where rules live | In `datasets.yaml`; or a new `rules.yaml` | A new, optional `rules.yaml`. Field-level meaning stays on the field |
| D2 | Catalog format version | Keep `yodb/v0.1` and add optional keys; or move to `yodb/v0.2` | Keep `v0.1`. The keys are additions, and an old YoDb refuses a file it does not understand |
| D3 | The word for a named filter | condition, segment, filter, definition | "condition". It reads naturally in a query: `{"condition": "paying"}` |
| D4 | Can agents see what an always-on rule filters on | Always; never; per rule | Per rule, shown by default, with `visible: false` to hide |
| D5 | SQL expressions in `sources.yaml` | Allow; or build a portable language first | Allow, for one table, checked by `yodb validate` |
| D6 | How context reaches YoDb | A `connect` argument; per query; both | A `connect` argument first. Per-query context only for hosts that serve many callers from one client |
| D7 | Sorting a field with stored codes | By stored value; by declared order; refuse | Refuse at first. Add declared order when someone needs it |
| D8 | Metrics | Build after row-level rules; build first; do not build | After. Decide once section 11's coverage count shows how many real rules are metrics |
| D9 | An alias used in a query | Resolve it silently; refuse with a suggestion | Resolve it, and report the real name in the result |
| D10 | Aggregation without a named metric | Never; a plain `count` only | A plain `count` of matching rows, since agents ask it constantly and it needs no definition. Everything else by name |
