# Business logic in YoDb: build path

**Status:** proposal. Nothing here is built.

**What this is:** the order in which to build the design in [business-logic.md](business-logic.md), in
stages small enough to ship one at a time. "Design section N" below refers to that file.

**Time estimates** are rough guesses for one person working part-time. They are there to show relative
size, and they are not commitments.

---

## 0. How to use this

### 0.1 Rules for every stage

1. **One stage, one pull request.** Each stage leaves `main` working and releasable.
2. **Tests first.** Write the failing tests, then the code. For anything that changes query results, add
   cases to the generated matrix (`tests/e2e/run_matrix.py`) so the SQL oracle checks it.
3. **A query that uses a rule must equal the hand-expanded query.** This is the core test for every
   rewrite stage: run both, compare rows and order.
4. **No stage changes an existing query's result.** A catalog with no rules behaves exactly as before.
   The existing suites prove it; they must stay green with no edits.
5. **New keys start as experimental.** Add each one to the experimental tier in
   `docs/reference/compatibility.mdx`. Move it to stable only after a gate.
6. **Docs in the same pull request.** A feature without its page is not done.
7. **Stop at the gates.** There are three. Each asks whether the next block is worth building.

### 0.2 The path at a glance

| Stage | Builds | Engine change? | Size |
| --- | --- | --- | --- |
| 0 | Groundwork: fix what rules would make worse | Small fixes | 1-2 weeks |
| 1 | Field meaning: allowed values, units, caveats | Validation only | 1 week |
| 2 | Named conditions | Rewrite only | 1-2 weeks |
| 3 | Always-on rules | Rewrite only | 1-2 weeks |
| 4 | `yodb check`, `yodb rules`, `yodb preview` | None | 1-2 weeks |
| **Gate A** | Do real rules fit? | | |
| 5 | Time: relative values, periods, time zone | Value handling | 1-2 weeks |
| 6 | Condition parameters and value groups | Rewrite only | 1 week |
| 7 | Context values, row access, field access and masking | Rewrite and result step | 2 weeks |
| 8 | Stored codes | Value translation | 1-2 weeks |
| 9 | Derived fields and "which source wins" | Compiler and assembly | 2-3 weeks |
| 10 | Discovery for agents at scale, glossary, `yodb docs`, `yodb diff` | None | 2 weeks |
| **Gate B** | Do agents answer better with rules? | | |
| 11 | Dataset policies: required filters, default order, deprecation | Validation only | 1 week |
| 12 | Metrics | **New engine feature** | 6-10 weeks, own design |
| **Gate C** | Is the next block needed? | | |
| 13 | Later: conditions across datasets, portable expressions, calendars | New operators | Not sized |

Stages 0 to 4 are the useful core: about 5 to 9 weeks, with no new engine feature. They are also the
part no other tool offers across several databases.

### 0.3 What each stage delivers to a user

| After stage | A catalog author can | An agent can |
| --- | --- | --- |
| 1 | Close a field's set of values | Get a clear error for a wrong value, not an empty answer |
| 2 | Name a filter once | Ask for "paying customers" by name |
| 3 | Guarantee some rows are never returned | Nothing new; it cannot get this wrong any more |
| 4 | Test and preview rules | Nothing new |
| 5 | Write "in the last 30 days" | Ask time questions without computing dates |
| 7 | Scope data per tenant and role | Nothing new; it is scoped whether it knows or not |
| 9 | Define computed fields | Filter and sort on them like any field |
| 12 | Define metrics | Ask "how many" and "what total" |

---

## Stage 0: Groundwork (1-2 weeks)

**Goal:** remove the problems that rules would make worse, and add the plumbing every later stage needs.

**Steps**

1. **Confirm and fix the pushdown problem.** Observed: with `starts_with` in an `all`, the sibling
   condition on `country` was no longer pushed to the database (`explain` showed no `pushed=`).
   - Write a failing test in `tests/test_planner_paths.py`: `all[country eq, name starts_with]` on one
     source must push `country`.
   - Find the cause in `planning/operators/scan.py` and `planning/operators/filter.py`. The planning page
     says each single-source condition is pushed, so the code and the page disagree.
   - Fix it, then re-run the matrix. If the behaviour is deliberate, fix the page and record why.
2. **Make aliases usable.** Decide design decision D9. Either resolve a dataset or field alias to its real
   name, or refuse with "did you mean". Add the same "did you mean" to `dataset_not_found` and
   `field_not_found` for near misses.
3. **Allow a catalog with no relationships.** Let `relationships` be empty in `catalog.py`, so a
   one-dataset catalog loads.
4. **Add a trusted binding path.** In `query/validation.py`, add a way to bind a field that accepts
   `visibility: internal`, beside `bind_public_field`. Nothing calls it yet. Test that the public path
   still refuses internal fields.
5. **Add "what was applied" to results.** Give `QueryExecutionResult` an `applied` mapping and
   `PlanExplanation` a matching field, both empty for now. Carry them through `cli.py` (`--format json`)
   and `mcp_server.py`. Add them to the compatibility page as experimental.
6. **Load the two new files.** Teach `load_catalog` to read optional `rules.yaml` and `checks.yaml` into
   the `Catalog` model with strict, empty-for-now models. Because they are part of `Catalog`, the catalog
   fingerprint covers them.

**Tests:** unit tests for each step; the full e2e set unchanged and green.

**Done when:** the pushdown test passes or the page is corrected; a catalog with an empty `rules.yaml`
loads and behaves as before; `--format json` shows an empty `applied`.

**Watch out:** step 1 can change plans for existing queries. That is intended, but check the matrix's
"plan shapes exercised" report before and after.

---

## Stage 1: Field meaning (1 week)

**Goal:** a wrong value is refused, not answered with nothing. Design section 5.2.

**Steps**

1. Add to `FieldSpec` in `catalog.py`: `allowed_values`, `unit`, `null_means`, `caveats`. Check when the
   catalog loads that every allowed value has the field's type, and that `example_values` are within
   `allowed_values` when both are given.
2. In `_normalize_scalar` (`query/validation.py`), refuse a filter value outside `allowed_values` with a
   new code, `value_not_allowed`. The message lists the allowed values, up to a fixed number.
3. Show the new keys in `describe_catalog` (`mcp_server.py`), `db.describe()` and `yodb catalog`.
4. Return a field's `caveats` under `notes` in a result that selected or filtered on it.
5. Optional: have `yodb validate` warn when a column holds a value outside `allowed_values`. It needs a
   `SELECT DISTINCT` under the row limit, so make it a separate flag (`--check-values`).

**Tests:** value accepted; value refused with the list; `in` with one bad value refused; a field with no
`allowed_values` unchanged; matrix cases for `value_not_allowed`.

**Done when:** `{"field": "country", "op": "eq", "value": "GB"}` returns
`error [value_not_allowed] at where.value` and names `US, UK, FI`.

**Watch out:** `contains` and `starts_with` take free text. Do not apply `allowed_values` to them.

---

## Stage 2: Named conditions (1-2 weeks)

**Goal:** a filter with a name, used by name. Design sections 5.3, 6.1 to 6.3.

**Steps**

1. **Model.** In `catalog.py`, add `rules.<dataset>.conditions.<name>` with `description` and `where`.
   No parameters yet.
2. **Check at load.** For each condition, parse its `where` with the query parser and bind it against its
   dataset. A condition that names a missing field, uses a wrong type, or refers to an unknown condition
   fails the load, with the path in the message. Detect loops between conditions.
3. **Parse.** In `query/parser.py`, accept `{"condition": "name"}` as a filter term.
4. **Expand.** New module `src/yodb/rules/expand.py`. Called from `bind_query`, it replaces each
   condition term with the bound filter of its definition, recursively, with a depth cap. The output
   contains only ordinary bound filters.
5. **Limits.** Apply the existing depth and term limits after expansion.
6. **Fingerprint.** Take the query fingerprint from the expanded filter.
7. **Joins.** Confirm that a condition in a `traverse` step's `where` binds to the traversed dataset. The
   join planner already plans each side as its own query, so this should need no change. Test it.
8. **Report.** Fill `applied.conditions` with each condition's name, dataset and meaning in logical
   terms. Show the same in `explain`.
9. **Discovery.** List each dataset's conditions in `describe_catalog`. Add one line to the MCP query
   guide: use a named condition when one exists.
10. **Errors.** `condition_not_found`, with near matches.

**Tests**
- A query with a condition returns exactly what the hand-expanded query returns (matrix category
  `condition`, compared with the oracle).
- A condition under `any` and under `not`.
- A condition that uses another; a loop refused at load.
- A condition whose fields are in two databases.
- A condition on each side of a join.
- A condition with a semantic term inside it is refused at load (keep the first version simple).

**Done when:** `{"from": {"dataset": "customer"}, "where": {"condition": "paying"}}` returns the same rows
as the `plan in [pro, team]` query, `explain` shows the expansion, and the matrix is green.

**Watch out:** an expanded filter must be planned exactly like a typed one. If `explain` for the two
differs in anything but the `applied` section, the expansion is leaking into the plan.

---

## Stage 3: Always-on rules (1-2 weeks)

**Goal:** rules the caller cannot forget or bypass. Design section 5.4.

**Steps**

1. **Model.** `rules.<dataset>.always`: a list of `name`, `description`, `where`, and `visible`
   (default `true`).
2. **Check at load.** Bind each rule's `where` through the trusted path from stage 0, so it may use
   internal fields. Check that every source of the dataset can supply the fields the rule needs.
3. **Apply.** In `rules/expand.py`, after conditions are expanded, AND every always-on rule of the
   dataset onto the query's filter. Do this inside the per-dataset binding, so it happens on both sides
   of a join.
4. **Source resolution.** An internal field used by a rule must be read from its source. Check that
   `query/resolution.py` includes it as a filter field and that it never appears in the result.
5. **Report.** Fill `applied.always`. For a rule with `visible: false`, report its name and description
   only. Make sure its field never appears in `explain`, in an error, or in the MCP output.
6. **Guard against bypass.** Confirm there is no query key that disables a rule, and that a rule still
   applies when the caller's own filter mentions the same field.

**Tests**
- A row excluded by a rule never appears: with no filter, with a filter that would match it, by ID, on
  either side of a join, in a left join, and with a semantic condition.
- A left join keeps a starting row whose only matches are excluded.
- A hidden rule's field name appears nowhere a caller can see. Search every output for it.
- The matrix runs every relational category again against a catalog that has an always-on rule, with the
  oracle's SQL carrying the same `WHERE`.

**Done when:** a soft-deleted row in the sample cannot be returned by any query the test suite can
generate.

**Watch out:** a rule on a field that only one source has, on a dataset spread over two, can change which
source must be read. Check row limits and plans for such a dataset.

---

## Stage 4: Tools for authors (1-2 weeks)

**Goal:** a person can write, see and test a rule without reading code. Design section 8.

**Steps**

1. **`yodb rules CATALOG [name]`.** Lists definitions per dataset. With a name, prints the full expansion
   and what uses it. No database needed.
2. **`yodb check CATALOG`.** Two parts:
   - **Lint, no database:** unknown names, loops, unused definitions, missing descriptions, clashing
     aliases, and rules that cannot be pushed to a database (for example an `any` across two sources, or
     a text operator), each with the reason.
   - **Tests, with a database:** run `checks.yaml`. Support `expect.ids`, `expect.count`,
     `expect.contains`, `expect.excludes`, and `disjoint` between two conditions.
   - Exit status 0 when clean, 1 on a failed check, so it can gate a merge.
3. **`yodb preview CATALOG DATASET --condition NAME`.** Prints a few rows that match and a few that do
   not, under the usual row limits.
4. **Better catalog errors.** Every catalog error names the file and the path, and suggests a near match
   for an unknown name.
5. **JSON Schema** for `datasets.yaml`, `sources.yaml`, `relations.yaml`, `rules.yaml` and `checks.yaml`,
   generated from the models and published in the repository, for editor completion.
6. **Docs:** a page "Business rules" under "Writing a catalog", and `rules.yaml` and `checks.yaml`
   reference pages.

**Tests:** each lint rule has a catalog that triggers it; `yodb check` on the sample catalog passes; a
deliberately wrong expectation fails with exit status 1.

**Done when:** the sample shop has a `rules.yaml` and a `checks.yaml`, `yodb check examples/shop/catalog`
passes in CI, and the quickstart shows one condition.

---

## Gate A: do real rules fit?

Do this before building further. It needs no code.

1. Collect 30 to 50 real business rules from one or two real teams, in their own words.
2. Sort each into the kinds in design section 4.
3. Count three things:

| Count | Continue | Adjust | Rethink |
| --- | --- | --- | --- |
| Rules that stages 1-4 can express | 40% or more | 20-40%: reorder the next stages toward what is missing | Under 20% |
| Rules that are metrics | Under 40% | 40-60%: move stage 12 earlier | Over 60%: metrics are the product; do stage 12 next |
| Rules nothing in the design can express | Under 15% | 15-30%: add the missing kinds to the design | Over 30% |

The thresholds are provisional. Write the counts and the decision in the pull request that closes this
gate.

Also ask each team to write five rules themselves with `yodb check` and `yodb preview`, and note where
they get stuck.

---

## Stage 5: Time (1-2 weeks)

**Goal:** rules and queries can say "in the last 30 days". Design section 5.6.

**Steps**

1. **Relative values.** In `_normalize_scalar`, accept `{"relative": "-30d"}` for a timestamp field. Units
   `h`, `d`, `w`, `mo`, `y`.
2. **One "now" per query.** Read the clock once at the start of `bind_query`, pass it down, and return it
   as `applied.now`. Allow it to be supplied for tests (`db.query(..., now=...)`), so results are
   repeatable.
3. **Fingerprint.** Fingerprint a relative value as written, not as resolved.
4. **Time settings.** `time.zone` and `time.fiscal_year_start` in `rules.yaml`.
5. **Named periods.** `{"period": "today" | "this_week" | "this_month" | "this_quarter" | "this_year" |
   "last_..." | "this_fiscal_year"}`. With `gte` or `gt` it means the period's start; with `lt` or `lte`,
   its end. Add a `during` operator only if the two-sided form proves awkward.
6. **Joins.** One "now" for both sides of a join.

**Tests:** fixed "now" in every test; month ends, leap years, daylight-saving changes in the configured
zone; fiscal year boundaries; the matrix with a supplied "now" compared with oracle SQL using the same
moment.

**Done when:** a condition `new` defined as `signed_up gte {"relative": "-30d"}` passes its check with a
supplied "now", and the result reports that moment.

**Watch out:** naive timestamps in a source are read as UTC today. Say so on the time page, because day
boundaries will otherwise surprise people.

---

## Stage 6: Parameters and value groups (1 week)

**Goal:** one condition covers a family of questions. Design sections 5.2 and 5.3.

**Steps**

1. **Parameters.** `parameters` on a condition: `type`, `default`, `minimum`, `maximum`, or
   `allowed_values`. Use `{name}` inside the condition's values. Check at load that every placeholder is
   declared and has the right type for where it is used.
2. **Use.** `{"condition": "new", "with": {"days": 7}}`. Refuse unknown, missing or out-of-range
   parameters with `condition_parameter_invalid`.
3. **Groups.** `groups` on a field. Accept `{"group": "europe"}` as the value of `in` and `not_in`. Expand
   to the list before validation, so the 1,000-value limit applies to the expanded list.
4. Show parameters and groups in discovery and in `yodb rules`.

**Tests:** defaults; bounds; a parameter used in a relative time; a group over the list limit refused at
load.

**Done when:** `{"condition": "new", "with": {"days": 7}}` equals the hand-written query.

---

## Stage 7: Context, row access and field access (2 weeks)

**Goal:** what a caller sees depends on who it is, and the caller cannot change that. Design sections
4.7 and 5.5.

**Steps**

1. **Context.** `yodb.connect(..., context={...})` and `yodb mcp --context key=value`. Values are typed
   and fixed for the life of the client.
2. **In rules.** Accept `{"context": "tenant"}` as a value in an always-on rule. A missing value refuses
   the query with `context_missing`. The parser refuses `{"context": ...}` in a caller's query.
3. **Field access.** `visible_to: [role, ...]` on a field. A field the caller's role may not see behaves
   exactly like an internal field: not listed, not selectable, not filterable.
4. **Masking.** `mask: null | hash | last4` on a field, applied to results after the query runs. A masked
   field cannot be filtered or sorted on, because that would leak it.
5. **Report.** `applied.always` names the row rule. Context values themselves are never returned.

**Tests:** two clients with different context over the same catalog see disjoint rows; a query cannot set
context; a masked field cannot be recovered by filtering, sorting, or joining on it; the MCP server with
and without context.

**Done when:** the multi-tenant test catalog returns only the caller's tenant for every generated query.

**Watch out:** masking is the first thing that changes result values after the query. Keep it to a small
fixed list of masks.

---

## Stage 8: Stored codes (1-2 weeks)

**Goal:** callers use `open`; the database stores `1`. Design section 4.1.

**Steps**

1. `codes: {open: 1, pending: 2, closed: 3}` on a field, in `sources.yaml`, because the stored value is a
   physical fact. The field's logical type and `allowed_values` stay in `datasets.yaml`.
2. Translate filter values from logical to stored before compiling. Translate result values back after
   reading.
3. A stored value with no code is returned as no value, and reported in `notes`.
4. Refuse sorting on a coded field (design decision D7).
5. `yodb validate --check-values` reports stored values that have no code.

**Tests:** every operator on a coded field against the oracle; a dataset whose two sources code the same
field differently.

**Done when:** `status eq "open"` reads `WHERE status = 1` in the database log and returns `open`.

---

## Stage 9: Derived fields and "which source wins" (2-3 weeks)

**Goal:** computed fields that filter and sort like real ones. Design section 5.7.

**Steps**

1. **Model.** In `sources.yaml`, a field mapping is either `physical_name` or `expression`, never both.
2. **Compile.** In `compilation/postgres.py`, write the expression in parentheses wherever a column is
   written today: in `SELECT`, `WHERE` and `ORDER BY`.
3. **Check at validation.** In `inspection/postgres.py`, run `SELECT (expression) FROM resource LIMIT 0`
   in the read-only session, and compare the result type with the field's logical type. Refuse anything
   that is not a single expression.
4. **Planner.** A filter on a derived field uses the default estimate. Note it in `explain`.
5. **Which source wins.** Let `field_sources.<field>` be a list. When merging rows, take the first source
   that has a value. A filter on such a field cannot be pushed to one source; say so in lint.
6. **Docs:** what expressions may contain, that they are written by the catalog author, and that `now()`
   in an expression is the database's clock, not the query's fixed "now".

**Tests:** filter, sort and select on a derived field against the oracle; an invalid expression refused
by `yodb validate` with the database's message made safe; an expression that tries a second statement
refused; a fallback field with values in the first source, the second, and neither.

**Done when:** `is_overdue` in the sample filters and sorts like a column, and `explain` shows it pushed.

**Watch out:** this is the first SQL written by a person that YoDb runs. It comes from the trusted
catalog, and sessions are read-only, but treat the validation step as a security boundary and test it
with hostile input.

---

## Stage 10: Discovery at scale, and tools for change (2 weeks)

**Goal:** an agent finds the right definition in a large catalog, and a person sees what a change will
do. Design sections 7.2 and 8.4.

**Steps**

1. **MCP tools.** Make `describe_catalog` a summary. Add `describe_dataset(name)` and
   `find_definitions(text)`. Search names, aliases, descriptions and glossary terms with plain text
   matching first.
2. **Glossary.** `glossary` in `rules.yaml`: a term, its meaning, and a pointer to a field, condition or
   metric.
3. **`yodb docs CATALOG`.** Writes a glossary page in Markdown for people.
4. **`yodb diff OLD NEW`.** Lists added, removed and changed definitions. With a database, reports for
   each changed condition how many rows enter and leave it, under the row limits.
5. **Deprecation and owners.** `deprecated: {replaced_by, remove_after}` and `owner` on any definition. A
   query that uses something deprecated gets a note; `yodb check` fails after the date.

**Tests:** the MCP end-to-end suite with the three tools; a catalog of 200 generated definitions stays
under a fixed size for `describe_catalog`.

**Done when:** an agent can answer a question that needs a condition it was not told about, by finding it
with `find_definitions`.

**Watch out:** changing what `describe_catalog` returns is a change to a stable surface. Follow the
deprecation rule: keep the full form available for one release.

---

## Gate B: do agents answer better?

1. Write 40 questions with known answers over a catalog that has rules. Mix: questions that need a
   condition, a time period, an excluded row, a wrong-value trap, and some with no matching rule.
2. Run an agent on two catalogs: descriptions only, and with rules.
3. Compare:

| Measure | Continue | Rethink |
| --- | --- | --- |
| Correct answers, rules against descriptions only | Clearly higher | No real difference |
| Wrong answers given with confidence | Clearly lower | No real difference |
| Share of questions where the agent used an existing condition | High | Low: fix discovery and the guide before anything else |
| Rows read and time per question | Not worse | Worse: find the rule that blocks pushdown |

If rules do not help the agent, more kinds of rule will not either. Find out why first.

---

## Stage 11: Dataset policies (1 week)

**Goal:** a dataset can say how it must be queried. Design section 4.8.

**Steps**

1. `require` on a dataset: a list of alternatives, each a set of fields that must be filtered. Refuse a
   query that satisfies none with `required_filter_missing`, naming the alternatives.
2. `default_order` on a dataset, used when a query gives no `order_by`.
3. A note on results when a join can repeat rows, taken from the relationship's `cardinality`.
4. `default: true` on a relationship, shown in discovery.

**Tests:** each alternative accepted; none refused; a condition that supplies the required filter counts.

**Done when:** the large test table cannot be queried without a date range or an ID.

---

## Stage 12: Metrics (6-10 weeks, own design first)

**Goal:** "how many" and "what total", by name. Design section 5.8.

This stage adds aggregation, which the engine does not have. It is the only stage that is a new engine
feature. Do not start it from this page.

**Before any code**

1. Write `plans/metrics.md` in the style of the existing `v0.1-*` plans, and review it. It must settle:
   - the query shape, and whether a plain `count` exists without a named metric (design decision D10);
   - which aggregates: `count`, `count_distinct`, `sum`, `avg`, `min`, `max`;
   - how a single-source metric is pushed down as `GROUP BY`;
   - what happens for a dataset spread over several databases, where rows must be merged before
     aggregating and the row limits apply;
   - dimensions from a related dataset, allowed only across a many-to-one relationship;
   - time grains, using the time settings from stage 5;
   - how always-on rules and conditions apply to a metric (they must);
   - what the oracle is for the matrix.
2. Take the decision at Gate A into account: how many real rules were metrics.

**Then, in order**

1. `count` of rows matching a filter, single source, pushed down.
2. The other aggregates on one field, single source.
3. `by` with declared dimensions, single source.
4. Named metrics in `rules.yaml` with a condition.
5. Datasets spread over several sources, under the row limits, refused with a clear error beyond them.
6. Time grains.
7. Ratios of two metrics.

**Done when:** every metric in the sample equals a hand-written SQL aggregate, the matrix has a `metric`
category against the oracle, and a metric over a join cannot be double-counted by any generated query.

---

## Gate C: is the next block needed?

Only start stage 13 for something a real user asked for twice. Record who asked and for what.

---

## Stage 13: Later

Not sized, and not designed here. Listed so the earlier stages do not block them.

| Item | Needs | Design section |
| --- | --- | --- |
| Conditions across datasets ("customers with an open ticket") | A semi-join operator in the engine | 4.2 |
| Derived fields across databases | A small portable expression language, evaluated by YoDb | 4.4 |
| Business days and holidays | A calendar table declared in the catalog | 4.3 |
| Sorting coded fields in logical order | Sorting by a mapped rank | 6.4 |
| Per-query context for hosts serving many callers | A trusted way to pass context per call | 5.5 |
| Freshness reported with results | A way to read each source's load time | 4.8 |

---

## 14. What each stage touches

| Stage | Main files |
| --- | --- |
| 0 | `planning/operators/scan.py`, `planning/operators/filter.py`, `query/validation.py`, `catalog.py`, `execution/contracts.py`, `planning/contracts.py`, `cli.py`, `mcp_server.py` |
| 1 | `catalog.py`, `query/validation.py`, `errors.py`, `mcp_server.py`, `client.py`, `cli.py` |
| 2 | `catalog.py`, `query/parser.py`, new `rules/expand.py`, `query/validation.py`, `mcp_server.py`, `errors.py` |
| 3 | `catalog.py`, `rules/expand.py`, `query/validation.py`, `query/resolution.py` |
| 4 | `cli.py`, new `rules/lint.py`, new `rules/checks.py`, `docs/` |
| 5 | `query/validation.py`, `query/parser.py`, `rules/expand.py`, `client.py`, `execution/engine.py` |
| 6 | `catalog.py`, `rules/expand.py`, `query/parser.py` |
| 7 | `client.py`, `cli.py`, `mcp_server.py`, `rules/expand.py`, `execution/engine.py` |
| 8 | `catalog.py`, `compilation/postgres.py`, `execution/postgres.py`, `inspection/postgres.py` |
| 9 | `catalog.py`, `compilation/postgres.py`, `inspection/postgres.py`, `execution/operators/combine.py` |
| 10 | `mcp_server.py`, `cli.py`, new `rules/diff.py` |
| 11 | `catalog.py`, `query/validation.py` |
| 12 | The planner, the operators, the compiler, and the executor. See its own plan |

---

## 15. Tests that run through every stage

| Suite | What it adds |
| --- | --- |
| Unit tests | Each construct's parse, load check, expansion and errors |
| Generated matrix (`run_matrix.py`) | A category per construct. Every query that uses a rule is compared with the hand-expanded query and with oracle SQL |
| Robustness (`run_robustness.py`) | Hostile condition names and parameters; hostile expressions in the catalog; attempts to set context from a query |
| MCP end-to-end (`run_mcp.py`) | Discovery tools; `applied` in answers; hidden rules never visible |
| A "rules do nothing by default" run | The whole existing suite with an empty `rules.yaml`, expecting identical output |
| `yodb check` on the sample | Run in CI on every pull request |

---

## 16. When to stop and reconsider

- A stage needs a change to the planner that was not listed: stop and write it down first. Stages 0 to 11
  are meant to be rewrites in front of the engine.
- A construct needs an "except when" clause to work: the construct is wrong. Redesign it.
- Two always-on rules need an order between them: they should not. Find what they are really expressing.
- An author needs an expression inside a query: collect the cases. That is evidence for stage 13, not a
  reason to allow it.
- The coverage count at Gate A is low: the kinds of rule in the design are wrong for real users. Fix the
  design before building more of it.
