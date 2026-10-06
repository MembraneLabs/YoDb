"""The operators YoDb knows about: one vocabulary every layer reads.

An *operator* is one kind of work a query can need (read, filter, order, combine
sources, answer a semantic condition, ...).  Each layer relates to the same
catalog:

* the **query** layer produces the logical terms an operator handles;
* the **planner** lists the legal *strategies* for each operator;
* the **adapters** declare which operators a database can run itself;
* the **physical plan** nodes name the operator they implement;
* the **executor** has one handler per operator.

Adding an operator means adding it here, then one planning operator, one plan
node and one execution handler.  Nothing else changes.
"""

from .kinds import (
    OPERATORS,
    OperatorCatalog,
    OperatorCategory,
    OperatorKind,
    OperatorSpec,
    OperatorStatus,
)

__all__ = [
    "OPERATORS",
    "OperatorCatalog",
    "OperatorCategory",
    "OperatorKind",
    "OperatorSpec",
    "OperatorStatus",
]
