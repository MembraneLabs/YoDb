"""JOIN: match the rows of two datasets over a declared relationship.

A join is not a new kind of read.  Each side is an ordinary planned query over one dataset
(so every pushdown, key transfer, guard and extension applies to it).  The *driving* side runs
once; its distinct join keys are sent to the *probing* side in batches (an ``in`` filter on the
probing dataset's join field, planned for each batch); the rows are then matched in memory.
The node keeps a template of the probing side so that explaining and fingerprinting a plan show
both sides; the plan for a real batch comes from ``probe_for``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from typing import ClassVar, Literal

from ...operators import OperatorKind
from ..capabilities import OperatorSupport, SourceCapabilities, SupportLevel, support_view
from ..contracts import PhysicalNode, PlanExplanationNode


@dataclass(frozen=True)
class JoinPolicy:
    """Bounds on the in-memory join (a query over them fails with a clear error)."""

    batch_size: int = 1_000                  # join keys sent to the probing side at a time
    maximum_driver_rows: int = 10_000        # rows the driving side may produce
    maximum_probe_rows: int = 50_000         # rows all probing batches together may produce
    maximum_joined_rows: int = 50_000        # rows the join may hold before ordering and paging

    def __post_init__(self) -> None:
        if min(self.batch_size, self.maximum_driver_rows, self.maximum_probe_rows, self.maximum_joined_rows) < 1:
            raise ValueError("join limits must be positive")


@dataclass(frozen=True)
class OrderSpec:
    column: str            # a key of the joined row: ``name`` or ``alias.name``
    descending: bool


@dataclass(frozen=True)
class HashJoin(PhysicalNode):
    """Run ``driver``, probe the other side in key batches, match in memory, order, page, project."""

    operator: ClassVar[OperatorKind] = OperatorKind.JOIN

    driver: PhysicalNode                       # the side that runs first, once
    probe: PhysicalNode                        # the other side, planned for a sample batch (explain, fingerprint)
    driver_side: Literal["left", "right"]      # left is the root dataset, right the traversed one
    driver_key: str                            # join field in the driving rows
    probe_key: str                             # join field in the probing rows
    relationship: str
    alias: str
    optional: bool                             # keep root rows that have no match (left join)
    left_columns: tuple[str, ...]              # output columns from the root dataset
    right_columns: tuple[str, ...]             # output columns from the traversed dataset (before the alias prefix)
    order: tuple[OrderSpec, ...]
    first: int
    policy: JoinPolicy
    key_description: str
    # The probing side for a real batch of keys and a row budget.  Not part of equality or the fingerprint.
    probe_for: Callable[[Sequence[object], int], PhysicalNode] = field(compare=False, repr=False, default=None)  # type: ignore[assignment]

    @property
    def columns(self) -> tuple[str, ...]:
        return (*self.left_columns, *(f"{self.alias}.{name}" for name in self.right_columns))

    def inputs(self) -> tuple[PhysicalNode, ...]:
        return (self.driver, self.probe)

    def with_inputs(self, inputs: tuple[PhysicalNode, ...]) -> PhysicalNode:
        driver, probe = inputs
        return replace(self, driver=driver, probe=probe)

    def shape(self) -> dict[str, object]:
        return {
            "kind": "hash_join",
            "relationship": self.relationship,
            "driver": self.driver_side,
            "optional": self.optional,
            "order": [(o.column, o.descending) for o in self.order],
            "first": self.first,
            "batch_size": self.policy.batch_size,
            "driver_plan": self.driver.shape(),
            "probe_plan": self.probe.shape(),
        }

    def describe(self) -> PlanExplanationNode:
        probe_side = "right" if self.driver_side == "left" else "left"
        return PlanExplanationNode(
            kind="hash_join",
            location="coordinator",
            fields=self.columns,
            ordering=tuple(f"{o.column} {'desc' if o.descending else 'asc'}" for o in self.order),
            limit=self.first,
            key_transfer_max_keys=self.policy.batch_size,
            detail=(
                f"relationship={self.relationship} as={self.alias} kind={'left' if self.optional else 'inner'}",
                f"on {self.key_description}",
                f"driving side ({self.driver_side}) is planned first; the probing side ({probe_side}) is shown for one "
                f"batch and runs once per batch of up to {self.policy.batch_size} keys",
            ),
        )


@support_view(OperatorKind.JOIN)
def _join_support(caps: SourceCapabilities) -> OperatorSupport:
    strategies = ("driver_left", "driver_right")
    return OperatorSupport(
        OperatorKind.JOIN, SupportLevel.COORDINATOR, strategies,
        "YoDb joins in memory; each side is read by its own source, the probing side restricted to a batch of keys",
    )
