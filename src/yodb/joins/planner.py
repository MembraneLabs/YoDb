"""Plan a ``traverse`` query: find the relationship, build the two sides, choose which one drives."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
import json
import re
from typing import Any
from uuid import UUID

from ..catalog import Catalog, LogicalType, Visibility
from ..errors import ErrorCode, ErrorDetail, QueryError
from ..planning import (
    FederatedPhysicalPlanner,
    HashJoin,
    JoinPolicy,
    OrderSpec,
    PhysicalNode,
    PlanExplanation,
    PlannedQuery,
    explain_plan,
    plan_fingerprint,
)
from ..query import QueryValidationPolicy, bind_query, parse_query, resolve_query_sources
from ..query.parser import _parse_order_by, _parse_page
from ..query.validation import _validate_page
from ..runtime import CatalogEvaluation

@dataclass(frozen=True)
class Traversal:
    """The ``traverse`` step of a query, as submitted."""

    relationship: str
    alias: str
    select: tuple[str, ...] | None
    where: object | None
    optional: bool
    direction: str                # "forward" or "reverse"


_ALIAS = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,39}$")
_STEP_KEYS = frozenset({"relationship", "as", "select", "where", "optional", "direction"})
_ROOT_KEYS = frozenset({"from", "select", "where", "order_by", "page", "constraints", "traverse"})
_SAMPLE = {
    LogicalType.ID: "x", LogicalType.STRING: "x", LogicalType.TEXT: "x", LogicalType.INT: 0, LogicalType.FLOAT: 0.0,
    LogicalType.BOOL: True, LogicalType.TIMESTAMP: "1970-01-01T00:00:00Z", LogicalType.UUID: "00000000-0000-0000-0000-000000000000",
}


def has_traverse(raw: Any) -> bool:
    return isinstance(raw, Mapping) and "traverse" in raw


class JoinPlanner:
    """Turns a query with one ``traverse`` step into a :class:`JoinPlan`."""

    def __init__(
        self,
        planner: FederatedPhysicalPlanner,
        *,
        validation_policy: QueryValidationPolicy = QueryValidationPolicy(),
        policy: JoinPolicy = JoinPolicy(),
    ) -> None:
        self._planner = planner
        self._policy = policy
        # The two sides are internal queries: they may read more rows than a caller may ask a page of,
        # and carry a batch of keys in an ``in`` filter.
        self._user_policy = validation_policy
        self._internal_policy = replace(
            validation_policy,
            maximum_page_size=max(validation_policy.maximum_page_size, policy.maximum_driver_rows + 1, policy.maximum_probe_rows + 1),
            maximum_in_values=max(validation_policy.maximum_in_values, policy.batch_size),
        )

    def plan(self, raw: Mapping[str, Any], active: CatalogEvaluation) -> PlannedQuery:
        root_raw, step = split_traverse(raw)
        catalog = active.catalog
        root_dataset = _root_dataset(root_raw)
        relationship_name, right_dataset, left_key, right_key = _resolve_relationship(catalog, root_dataset, step)
        first, maximum_results = self._page(root_raw)
        left_order, right_order = _split_order(root_raw.get("order_by"), step.alias)
        _check_order_fields(catalog, root_dataset, [n for n, _ in left_order], "")
        _check_order_fields(catalog, right_dataset, [n for n, _ in right_order], f"{step.alias}.")
        left_select = _extend(root_raw.get("select"), left_key, *(name for name, _ in left_order))
        right_select = _extend(step.select, right_key, *(name for name, _ in right_order))
        left_where, right_where = root_raw.get("where"), step.where
        left_constraints, right_constraints = _constraints(root_raw.get("constraints"), left_where, right_where)

        def sub(dataset, select, where, key, batch, rows, constraints):
            clauses = ([where] if where is not None else []) + (
                [{"field": key, "op": "in", "value": [_wire(v) for v in batch]}] if batch is not None else []
            )
            query: dict[str, Any] = {"from": {"dataset": dataset}, "page": {"first": rows}}
            if select is not None:
                query["select"] = select
            if clauses:
                query["where"] = clauses[0] if len(clauses) == 1 else {"all": clauses}
            if constraints:
                query["constraints"] = constraints
            return query

        left_driver_query = sub(root_dataset, left_select, left_where, left_key, None, self._policy.maximum_driver_rows + 1, left_constraints)
        right_driver_query = sub(right_dataset, right_select, right_where, right_key, None, self._policy.maximum_driver_rows + 1, right_constraints)
        left_driver = self._plan(left_driver_query, active, "")
        right_driver = self._plan(right_driver_query, active, "traverse[0].")
        driver, notes = self._choose_driver(step, left_driver, right_driver, left_where, right_where)

        def probe_for(batch: Sequence[object], rows: int) -> PhysicalNode:
            """The other side, restricted to a batch of the driving side's join keys."""

            if driver == "left":
                query = sub(right_dataset, right_select, right_where, right_key, batch, rows, right_constraints)
                return self._plan(query, active, "traverse[0].").plan
            return self._plan(sub(root_dataset, left_select, left_where, left_key, batch, rows, left_constraints), active, "").plan

        probe_dataset, probe_key = (right_dataset, right_key) if driver == "left" else (root_dataset, left_key)
        sample = [_SAMPLE[catalog.datasets[probe_dataset].fields[probe_key].type]]
        probe_template = probe_for(sample, self._policy.maximum_probe_rows + 1)
        driver_plan, driver_key = (left_driver, left_key) if driver == "left" else (right_driver, right_key)

        node = HashJoin(
            driver=driver_plan.plan,
            probe=probe_template,
            driver_side=driver,
            driver_key=driver_key,
            probe_key=probe_key,
            relationship=relationship_name,
            alias=step.alias,
            optional=step.optional,
            left_columns=_columns(left_driver, root_raw.get("select")),
            right_columns=_columns(right_driver, step.select),
            order=tuple(
                OrderSpec(name if side == "left" else f"{step.alias}.{name}", descending)
                for side, name, descending in _ordered(root_raw.get("order_by"), step.alias)
            ),
            first=min(first, maximum_results) if maximum_results else first,
            policy=self._policy,
            key_description=f"{root_dataset}.{left_key} = {right_dataset}.{right_key}",
            probe_for=probe_for,
        )
        fingerprint = plan_fingerprint(node)
        return PlannedQuery(
            query=left_driver.query,                 # the root side's bound query; the join adds its own spec
            resolved=left_driver.resolved,
            plan=node,
            catalog_fingerprint=left_driver.catalog_fingerprint,
            query_fingerprint=_digest({
                "join": relationship_name, "alias": step.alias, "optional": step.optional, "direction": step.direction,
                "left": left_driver.query_fingerprint, "right": right_driver.query_fingerprint,
                "order": [(o.column, o.descending) for o in node.order], "first": node.first,
            }),
            plan_fingerprint=fingerprint,
            explain=PlanExplanation(
                plan_kind="hash_join",
                catalog_fingerprint=left_driver.catalog_fingerprint,
                plan_fingerprint=fingerprint,
                nodes=explain_plan(node),
                optimizer=notes,
            ),
        )

    # --- internals ---------------------------------------------------------------------------

    def _page(self, root_raw: Mapping[str, Any]) -> tuple[int, int | None]:
        page = _parse_page(root_raw["page"]) if "page" in root_raw else None
        if page is not None and page.after is not None:
            _fail(ErrorCode.QUERY_FEATURE_NOT_SUPPORTED, "Cursor execution is unavailable until signed cursor verification is implemented.", "page.after")
        first = _validate_page(page, self._user_policy).first
        constraints = root_raw.get("constraints")
        maximum = constraints.get("maximum_results") if isinstance(constraints, Mapping) else None
        if maximum is not None and (isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1):
            _fail(ErrorCode.QUERY_LIMIT_INVALID, "constraints.maximum_results must be a positive integer.", "constraints.maximum_results")
        return first, maximum

    def _plan(self, raw: dict[str, Any], active: CatalogEvaluation, prefix: str) -> PlannedQuery:
        try:
            bound = bind_query(parse_query(raw), active, policy=self._internal_policy)
            return self._planner.plan(resolve_query_sources(bound, active))
        except QueryError as error:
            detail = error.detail
            location = detail.location
            if location is not None and location.startswith(("from", "where", "select", "constraints")):
                location = f"{prefix}{location}" if prefix else location
            raise QueryError(replace_detail(detail, location)) from error

    def _choose_driver(self, step: Traversal, left: PlannedQuery, right: PlannedQuery, left_where, right_where) -> tuple[str, tuple[str, ...]]:
        """Which side to run first: the one expected to produce fewer rows (a left join always starts at the root)."""

        if step.optional:
            return "left", ("strategy=rules", "driver=left", "reason=a left join keeps every root row, so the root side drives")
        statistics = getattr(self._planner, "statistics", None)
        left_rows, right_rows = _estimate(left, statistics), _estimate(right, statistics)
        if left_rows is not None and right_rows is not None:
            driver = "left" if left_rows <= right_rows else "right"
            return driver, (
                "strategy=cost_based", f"driver={driver}",
                f"estimated_root_rows={left_rows:.0f}", f"estimated_traversed_rows={right_rows:.0f}",
            )
        left_filters, right_filters = _leaves(left_where), _leaves(right_where)
        if right_filters > left_filters:
            return "right", ("strategy=rules", "driver=right", f"reason=no statistics; the traversed side has more filters ({right_filters} against {left_filters})")
        return "left", ("strategy=rules", "driver=left", "reason=no statistics; the root side drives unless the traversed side has more filters")


# --- parsing the traverse step -------------------------------------------------------------------


def split_traverse(raw: Mapping[str, Any]) -> tuple[dict[str, Any], Traversal]:
    unknown = set(raw) - _ROOT_KEYS
    if unknown:
        _fail(ErrorCode.QUERY_SHAPE_INVALID, f"Unknown query key '{sorted(unknown)[0]}'.", sorted(unknown)[0])
    steps = raw["traverse"]
    if not isinstance(steps, list) or not steps:
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "'traverse' must be a list with one step.", "traverse")
    if len(steps) > 1:
        _fail(ErrorCode.QUERY_FEATURE_NOT_SUPPORTED, "V0.1 supports one traversal step per query.", "traverse")
    step = steps[0]
    if not isinstance(step, Mapping):
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "A traversal step must be an object.", "traverse[0]")
    unknown = set(step) - _STEP_KEYS
    if unknown:
        _fail(ErrorCode.QUERY_SHAPE_INVALID, f"Unknown traversal key '{sorted(unknown)[0]}'.", f"traverse[0].{sorted(unknown)[0]}")
    relationship = step.get("relationship")
    if not isinstance(relationship, str) or not relationship.strip():
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "A traversal requires a relationship name.", "traverse[0].relationship")
    alias = step.get("as", relationship)
    if not isinstance(alias, str) or not _ALIAS.match(alias):
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "'as' must be a name of letters, digits and underscores.", "traverse[0].as")
    select = step.get("select")
    if select is not None and (not isinstance(select, list) or not all(isinstance(s, str) and s for s in select) or len(set(select)) != len(select)):
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "'select' must be a list of distinct field names.", "traverse[0].select")
    optional = step.get("optional", False)
    if not isinstance(optional, bool):
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "'optional' must be true or false.", "traverse[0].optional")
    direction = step.get("direction", "forward")
    if direction not in ("forward", "reverse"):
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "'direction' must be 'forward' or 'reverse'.", "traverse[0].direction")
    root = {key: value for key, value in raw.items() if key != "traverse"}
    return root, Traversal(relationship, alias, tuple(select) if select is not None else None, step.get("where"), optional, direction)


def _root_dataset(root_raw: Mapping[str, Any]) -> str:
    source = root_raw.get("from")
    name = source.get("dataset") if isinstance(source, Mapping) else None
    if not isinstance(name, str) or not name:
        _fail(ErrorCode.QUERY_SHAPE_INVALID, "A query requires 'from.dataset'.", "from")
    return name


def _resolve_relationship(catalog: Catalog, root: str, step: Traversal) -> tuple[str, str, str, str]:
    """(relationship name, traversed dataset, root join field, traversed join field)."""

    found = next(
        ((name, rel) for name, rel in catalog.relationships.items() if step.relationship == name or step.relationship in rel.aliases),
        None,
    )
    if found is None:
        _fail(ErrorCode.RELATIONSHIP_NOT_FOUND, f"Unknown relationship '{step.relationship}'.", "traverse[0].relationship")
    name, rel = found
    if step.direction == "forward":
        if rel.from_dataset != root:
            _fail(ErrorCode.RELATIONSHIP_NOT_APPLICABLE,
                  f"Relationship '{name}' goes from '{rel.from_dataset}' to '{rel.to_dataset}', not from '{root}'"
                  + ("; it is declared bidirectional, so use \"direction\": \"reverse\"." if rel.direction == "bi" and rel.to_dataset == root else "."),
                  "traverse[0].relationship")
    else:
        if rel.direction != "bi":
            _fail(ErrorCode.RELATIONSHIP_NOT_APPLICABLE, f"Relationship '{name}' is not declared bidirectional, so it cannot be traversed in reverse.", "traverse[0].direction")
        if rel.to_dataset != root:
            _fail(ErrorCode.RELATIONSHIP_NOT_APPLICABLE, f"Relationship '{name}' does not end at '{root}'.", "traverse[0].relationship")
    implementation = next((i for i in rel.implementations if i.edge_type is None), None)
    if implementation is None:
        _fail(ErrorCode.QUERY_FEATURE_NOT_SUPPORTED, f"Relationship '{name}' is only implemented as a graph edge, which V0.1 cannot traverse.", "traverse[0].relationship")
    forward = step.direction == "forward"
    right_dataset = rel.to_dataset if forward else rel.from_dataset
    left_field = implementation.from_endpoint.field if forward else implementation.to_endpoint.field
    right_field = implementation.to_endpoint.field if forward else implementation.from_endpoint.field
    for dataset, field in ((root, left_field), (right_dataset, right_field)):
        if catalog.datasets[dataset].fields[field].visibility is not Visibility.PUBLIC:
            _fail(ErrorCode.FIELD_NOT_ACCESSIBLE, f"Relationship '{name}' joins on '{dataset}.{field}', which is internal; V0.1 can only join on public fields.", "traverse[0].relationship")
    return name, right_dataset, left_field, right_field


def _split_order(order_by: Any, alias: str) -> tuple[list[tuple[str, bool]], list[tuple[str, bool]]]:
    left, right = [], []
    for side, name, descending in _ordered(order_by, alias):
        (left if side == "left" else right).append((name, descending))
    return left, right


def _ordered(order_by: Any, alias: str) -> list[tuple[str, str, bool]]:
    if order_by is None:
        return []
    terms = _parse_order_by(order_by)
    result = []
    for term in terms:
        field = term.field
        descending = term.direction.value == "desc"
        if field.startswith(alias + "."):
            result.append(("right", field[len(alias) + 1:], descending))
        elif "." in field:
            _fail(ErrorCode.FIELD_NOT_FOUND, f"Unknown field '{field}'; fields of the traversed dataset are written '{alias}.<field>'.", "order_by")
        else:
            result.append(("left", field, descending))
    return result


def _check_order_fields(catalog: Catalog, dataset: str, names: list[str], prefix: str) -> None:
    fields = catalog.datasets[dataset].fields
    for name in names:
        spec = fields.get(name)
        if spec is None:
            _fail(ErrorCode.FIELD_NOT_FOUND, f"Unknown field '{prefix}{name}' on dataset '{dataset}'.", "order_by")
        if spec.visibility is not Visibility.PUBLIC:
            _fail(ErrorCode.FIELD_NOT_ACCESSIBLE, f"Field '{prefix}{name}' is not accessible.", "order_by")


def _extend(select: Any, *needed: str) -> list[str] | None:
    """The user's select with the fields the join needs added (None means every public field)."""

    if select is None:
        return None
    if not isinstance(select, (list, tuple)):
        return select      # the query parser reports the malformed select
    return list(dict.fromkeys([*select, *needed]))


def _constraints(raw: Any, left_where: Any, right_where: Any) -> tuple[dict | None, dict | None]:
    """Caller constraints for each side: the quality bar only to a side with a semantic condition,
    and ``maximum_results`` never (it is the join's page)."""

    if not isinstance(raw, Mapping):
        return None, None
    base = {k: v for k, v in raw.items() if k not in ("maximum_results", "minimum_quality")}
    quality = {"minimum_quality": raw["minimum_quality"]} if "minimum_quality" in raw else {}
    left = {**base, **(quality if _has_semantic(left_where) else {})}
    right = {**base, **(quality if _has_semantic(right_where) else {})}
    return left or None, right or None


def _has_semantic(where: Any) -> bool:
    if isinstance(where, Mapping):
        return "semantic" in where or any(_has_semantic(v) for v in where.values())
    if isinstance(where, list):
        return any(_has_semantic(v) for v in where)
    return False


def _leaves(where: Any) -> int:
    if isinstance(where, Mapping):
        if "field" in where or "semantic" in where:
            return 1
        return sum(_leaves(v) for v in where.values())
    if isinstance(where, list):
        return sum(_leaves(v) for v in where)
    return 0


def _columns(planned: PlannedQuery, user_select: Any) -> tuple[str, ...]:
    """Output columns of one side: what the caller selected, not the fields added for the join."""

    names = [field.name for field in planned.query.select]
    if user_select is not None:
        wanted = set(user_select) | {"id"}
        names = [name for name in names if name in wanted]
    return ("id", *(name for name in names if name != "id")) if "id" in names else tuple(names)       # id first, as in any result


def _estimate(planned: PlannedQuery, statistics) -> float | None:
    """Rows the side is expected to produce: the most selective scan that bounds it."""

    if statistics is None:
        return None
    scans = []

    def visit(node: PhysicalNode) -> None:
        from ..planning import RecordAssembly, RemoteScan

        if isinstance(node, RecordAssembly):
            scans.append(node.anchor)
            scans.extend(c for c in node.contributors if c.pushed_filter is not None)
        elif isinstance(node, RemoteScan):
            scans.append(node)
        else:
            for child in node.inputs():
                visit(child)

    visit(planned.plan)
    sizes = []
    for scan in scans:
        estimate = statistics.estimate_scan(scan.source, scan.pushed_filter)
        if not estimate.known or estimate.filtered_rows is None:
            return None
        sizes.append(estimate.filtered_rows)
    return min(sizes) if sizes else None


def _wire(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return value


def _digest(payload: object) -> str:
    return sha256(json.dumps(payload, sort_keys=True, default=repr).encode()).hexdigest()


def _fail(code: ErrorCode, message: str, location: str | None = None) -> None:
    raise QueryError(ErrorDetail(code=code, message=message, retryable=False, location=location))


def replace_detail(detail: ErrorDetail, location: str | None) -> ErrorDetail:
    return detail.model_copy(update={"location": location})
