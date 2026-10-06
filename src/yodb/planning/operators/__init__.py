"""Planning operators: one module per operator, assembled by the planner.

The relational spine (scan, combine, filter, order/page, project) is fixed;
extension operators plug into the slot after filtering by
implementing :class:`ExtensionOperator`.  See ``plans/v0.1-operators.md``.
"""

from .base import Claim, ExtensionOperator, ExtensionPlan, PlanningExtension, PlanningServices, Strategy, effective_limit
from .combine import CombineDecision, CombineOperator
from .filter import FilterOperator
from .join import HashJoin, JoinPolicy, OrderSpec
from .order_page import OrderPageOperator
from .project import ProjectOperator
from .scan import ScanOperator, ScanPlan

__all__ = [
    "Claim",
    "CombineDecision",
    "CombineOperator",
    "ExtensionOperator",
    "ExtensionPlan",
    "FilterOperator",
    "HashJoin",
    "JoinPolicy",
    "OrderSpec",
    "OrderPageOperator",
    "PlanningExtension",
    "PlanningServices",
    "ProjectOperator",
    "ScanOperator",
    "ScanPlan",
    "Strategy",
    "effective_limit",
]
