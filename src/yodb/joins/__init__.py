"""Joins between two datasets over a relationship declared in ``relations.yaml``.

``JoinPlanner`` turns a query with a ``traverse`` step into an ordinary :class:`PlannedQuery`
whose plan is a :class:`~yodb.planning.HashJoin`; the executor runs it like any other plan.
"""

from .planner import JoinPlanner, Traversal, has_traverse, split_traverse

__all__ = ["JoinPlanner", "Traversal", "has_traverse", "split_traverse"]
