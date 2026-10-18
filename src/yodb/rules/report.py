"""What a query reports about the rules it used."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from ..query.models import RuleUse


def merge_uses(*groups: Iterable[RuleUse]) -> tuple[RuleUse, ...]:
    """The rules of several bound queries (the two sides of a join) as one list, each rule once."""

    merged: dict[str, RuleUse] = {}
    for group in groups:
        for use in group:
            seen = merged.get(use.name)
            merged[use.name] = use if seen is None else replace(seen, named_in_query=seen.named_in_query or use.named_in_query)
    return tuple(merged.values())


def applied(uses: Iterable[RuleUse]) -> dict[str, Any]:
    """The ``applied`` entry of a result: the visible rules the query named, with their definitions.

    A rule that was only reached through another rule is left out (``explain`` shows it), and so is a
    hidden one.
    """

    return {
        "rules": [
            {"name": use.name, "dataset": use.dataset, "definition": use.definition}
            for use in uses
            if use.visible and use.named_in_query
        ]
    }
