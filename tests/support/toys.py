"""A toy extension, used to prove a second operator plugs in without touching the core."""

from __future__ import annotations

from dataclasses import dataclass

from yodb.operators import OperatorKind
from yodb.planning import Claim, ExtensionPlan, PlanExplanationNode, Strategy, UnaryNode, Variant
from yodb.planning.contracts import properties_from
from yodb.planning.optimizer import Estimate
from yodb.query.models import BoundAllExpression, BoundPredicate


@dataclass(frozen=True)
class KeepEqual(UnaryNode):
    """Toy operator: keep rows whose ``field`` equals ``value`` (applied by YoDb)."""

    input: object
    field: str
    value: object
    mode: str
    properties: object
    operator = OperatorKind.DISTINCT   # any kind; this one is not implemented by anything else

    def shape(self):
        return {"kind": "keep_equal", "input": self.input.shape(), "field": self.field, "mode": self.mode}

    def describe(self):
        return PlanExplanationNode("keep_equal", "coordinator", (), detail=(f"mode={self.mode}",))


def keep_equal_handler(ctx, node: KeepEqual, run):
    return tuple(row for row in ctx.execute(node.input, run) if row.get(node.field) == node.value)


class Plugin:
    """What an extension hands the engine: a planning operator and execution handlers."""

    def __init__(self, operator, handlers=None) -> None:
        self._operator = operator
        self._handlers = handlers if handlers is not None else {KeepEqual: keep_equal_handler}

    def planning_operator(self, services):
        return self._operator

    def handlers(self):
        return self._handlers


class SubjectToy:
    """Claims ``subject = X`` and offers a cheap and an expensive way to apply it."""

    kind = OperatorKind.DISTINCT

    def claim(self, where):
        if isinstance(where, BoundAllExpression):
            hits = [e for e in where.expressions if isinstance(e, BoundPredicate) and e.field.name == "subject"]
            rest = tuple(e for e in where.expressions if e not in hits)
            if hits:
                return Claim(tuple(hits), rest[0] if len(rest) == 1 else BoundAllExpression(rest) if rest else None)
        if isinstance(where, BoundPredicate) and where.field.name == "subject":
            return Claim((where,), None)
        return None

    def plan(self, claim, core, scans, query):
        (term,) = claim.terms

        def strategy(mode, latency):
            def build(node, notes):
                return KeepEqual(node, "subject", term.value, mode, properties_from(node.properties))

            return Strategy(Variant(mode, cost=lambda c: Estimate(latency, 0.0), payload=mode), build)

        slow, cheap = strategy("slow", 900.0), strategy("cheap", 5.0)
        notes = lambda chosen: (f"chose {chosen.variant.name if chosen else 'default'}",)  # noqa: E731
        return ExtensionPlan(self.kind, (slow, cheap), default=slow, notes_for=notes)
