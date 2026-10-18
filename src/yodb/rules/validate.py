"""Check a catalog's rules when it loads, so a broken rule never reaches a query."""

from __future__ import annotations

from difflib import get_close_matches

from ..catalog import Catalog, CatalogValidationError, _validate_identifier
from ..errors import QueryError
from ..query.models import BoundDataset
from ..query.parser import parse_filter
from ..query.registry import DEFAULT_TERMS
from ..query.validation import QueryValidationPolicy, bind_filter
from .expand import (
    CONDITION_TERMS,
    MAXIMUM_CONDITION_DEPTH,
    MAXIMUM_CONDITION_TERMS,
    RuleExpansion,
    check_expanded_size,
)


def _extension_key(raw: object) -> str | None:
    """The first extension term (such as a semantic condition) written anywhere inside a condition."""

    pending = [raw]
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                if key in DEFAULT_TERMS.keys():
                    return key
                pending.append(value)
        elif isinstance(node, (list, tuple)):
            pending.extend(node)
    return None


def validate_rules(catalog: Catalog) -> None:
    if not catalog.rules:
        return

    field_names = {field for dataset in catalog.datasets.values() for field in dataset.fields}
    seen: dict[str, str] = {}
    for dataset, rules in catalog.rules.items():
        if dataset not in catalog.datasets:
            near = get_close_matches(dataset, list(catalog.datasets), n=1)
            hint = f"; did you mean '{near[0]}'?" if near else ""
            raise CatalogValidationError(f"rules.yaml: rules.{dataset}: there is no dataset '{dataset}'{hint}")
        for rule in rules:
            where = f"rules.yaml: rules.{dataset}.{rule.name}"
            _validate_identifier(f"rules.{dataset}", rule.name)
            if rule.name in seen:
                raise CatalogValidationError(
                    f"{where}: the name is already used by a rule on '{seen[rule.name]}'; rule names are unique"
                )
            seen[rule.name] = dataset
            # A hidden rule is never offered to a caller, so it may share a name with a column.
            if rule.visible and rule.name in field_names:
                raise CatalogValidationError(
                    f"{where}: the name is also a field name; rename the rule, or make it 'visible: false'"
                )

    policy = QueryValidationPolicy()
    for dataset, rules in catalog.rules.items():
        root = BoundDataset(name=dataset, scope=dataset, spec=catalog.datasets[dataset])
        for rule in rules:
            where = f"rules.yaml: rules.{dataset}.{rule.name}"
            expansion = RuleExpansion(catalog)
            expansion.begin(rule.name)
            extension = _extension_key(rule.condition)
            if extension is not None:
                raise CatalogValidationError(f"{where}.condition: a rule cannot hold a '{extension}' condition")
            try:
                condition = parse_filter(
                    rule.condition,
                    "condition",
                    CONDITION_TERMS,
                    maximum_depth=MAXIMUM_CONDITION_DEPTH,
                    maximum_terms=MAXIMUM_CONDITION_TERMS,
                )
                bound = bind_filter(condition, root, "condition", policy, CONDITION_TERMS, expansion)
                check_expanded_size(bound, "condition")
            except QueryError as error:
                detail = error.detail
                raise CatalogValidationError(f"{where}.{detail.location or 'condition'}: {detail.message}") from error
