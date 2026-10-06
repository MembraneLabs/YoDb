"""Customers and tickets in four sources, with relationships between them (for join tests)."""

from __future__ import annotations

from support.sql_world import SqlWorld, evaluation

DATASETS = """\
api_version: yodb/v0.1
catalog: {name: shop, version: 1}
datasets:
  customer:
    description: A customer.
    fields:
      id:          {type: id,     description: Identity.}
      name:        {type: string, description: Name.}
      country:     {type: string, description: Country.}
      plan:        {type: string, description: Billing plan.}
      referrer_id: {type: id,     description: The customer that referred this one.}
      seats:       {type: int,    description: Seats.}
  ticket:
    description: A support ticket.
    fields:
      id:          {type: id,     description: Identity.}
      customer_id: {type: id,     description: Owning customer.}
      subject:     {type: string, description: Subject.}
      status:      {type: string, description: Status.}
      priority:    {type: int,    description: Priority.}
      assignee:    {type: string, description: Assignee.}
      legacy_key:  {type: id,     description: An old key., visibility: internal}
      body:        {type: text,   description: Ticket text., semantic_eligible: true}
"""
SOURCES = """\
api_version: yodb/v0.1
sources:
  crm:
    kind: postgres
    connection_ref: crm
    read_only: true
    datasets:
      customer:
        resource: crm.customers
        identity: [id]
        fields:
          id: {physical_name: customer_id}
          name: {physical_name: name}
          country: {physical_name: country}
          referrer_id: {physical_name: referrer_id}
          seats: {physical_name: seats}
  billing:
    kind: postgres
    connection_ref: billing
    read_only: true
    datasets:
      customer:
        resource: billing.plans
        identity: [id]
        fields:
          id: {physical_name: customer_id}
          plan: {physical_name: plan}
  desk:
    kind: postgres
    connection_ref: desk
    read_only: true
    datasets:
      ticket:
        resource: desk.tickets
        identity: [id]
        fields:
          id: {physical_name: ticket_id}
          customer_id: {physical_name: customer_id}
          subject: {physical_name: subject}
          status: {physical_name: status}
          priority: {physical_name: priority}
          legacy_key: {physical_name: legacy_key}
          body: {physical_name: body}
  triage:
    kind: postgres
    connection_ref: triage
    read_only: true
    datasets:
      ticket:
        resource: triage.assignments
        identity: [id]
        fields:
          id: {physical_name: ticket_id}
          assignee: {physical_name: assignee}
resolution:
  customer:
    identity_source: crm
    field_sources: {id: crm, name: crm, country: crm, referrer_id: crm, seats: crm, plan: billing}
  ticket:
    identity_source: desk
    field_sources: {id: desk, customer_id: desk, subject: desk, status: desk, priority: desk, legacy_key: desk, body: desk, assignee: triage}
"""
RELATIONS = """\
api_version: yodb/v0.1
relationships:
  customer_has_ticket:
    from: customer
    to: ticket
    description: A ticket belongs to a customer.
    aliases: [tickets]
    cardinality: one_to_many
    direction: uni
    implementations:
      - from: {source: crm, field: id}
        to: {source: desk, field: customer_id}
  customer_ticket_both_ways:
    from: customer
    to: ticket
    description: The same link, traversable from either end.
    cardinality: one_to_many
    direction: bi
    implementations:
      - from: {source: crm, field: id}
        to: {source: desk, field: customer_id}
  referred_by:
    from: customer
    to: customer
    description: A customer was referred by another.
    cardinality: many_to_one
    direction: uni
    implementations:
      - from: {source: crm, field: referrer_id}
        to: {source: crm, field: id}
  customer_legacy_link:
    from: customer
    to: ticket
    description: Joins on an internal field, which V0.1 refuses.
    cardinality: one_to_many
    direction: uni
    implementations:
      - from: {source: crm, field: id}
        to: {source: desk, field: legacy_key}
"""

CUSTOMERS = [
    # id, name, country, referrer_id, seats
    ("c1", "Ann", "US", None, 10), ("c2", "Bob", "UK", "c1", 5), ("c3", "Cai", "US", "c1", 20),
    ("c4", "Dee", None, "c2", 1), ("c5", "Eli", "DE", None, 7), ("c6", "Fay", "US", "c3", 3),
    ("c7", "Gus", "UK", None, 9), ("c8", "Hal", "DE", "c5", 2),
]
PLANS = [("c1", "pro"), ("c2", "basic"), ("c3", "pro"), ("c4", "basic"), ("c5", "pro"), ("c6", "basic"), ("c7", "pro")]   # c8 has none
TICKETS = [
    # id, customer_id, subject, status, priority, legacy_key, body
    ("t1", "c1", "Login", "open", 3, "k1", "cannot login"), ("t2", "c1", "Invoice", "closed", 1, "k2", "refund the invoice"),
    ("t3", "c1", "Crash", "open", 5, "k3", "app crash"), ("t4", "c2", "Slow", "open", 2, "k4", "slow pages"),
    ("t5", "c3", "Export", "closed", 4, "k5", "export fails"), ("t6", "c3", "Refund", "open", 4, "k6", "please refund me"),
    ("t7", "c4", "Billing", "pending", 1, "k7", "billing question"), ("t8", "c5", "Outage", "open", 5, "k8", "total outage"),
    ("t9", "c5", "Feature", "open", 3, "k9", "feature idea"), ("t10", "c6", "Thanks", "closed", 2, "k10", "thank you"),
    ("t11", None, "Orphan", "open", 1, "k11", "no customer"), ("t12", "c99", "Ghost", "open", 2, "k12", "unknown customer"),
    ("t13", "c1", "Upgrade", "pending", 5, "k13", "refund after upgrade"), ("t14", "c2", "Cancel", "closed", 3, "k14", "cancel and refund"),
]
ASSIGNMENTS = [("t1", "ann"), ("t3", "ann"), ("t4", "bob"), ("t6", "bob"), ("t8", "ann"), ("t13", "bob")]


def shop_active():
    return evaluation(DATASETS, SOURCES, RELATIONS)


def shop_world() -> SqlWorld:
    return SqlWorld({
        "crm.customers": (("customer_id", "name", "country", "referrer_id", "seats"), CUSTOMERS),
        "billing.plans": (("customer_id", "plan"), PLANS),
        "desk.tickets": (("ticket_id", "customer_id", "subject", "status", "priority", "legacy_key", "body"), TICKETS),
        "triage.assignments": (("ticket_id", "assignee"), ASSIGNMENTS),
    })


def customer_rows() -> list[dict]:
    plans = dict(PLANS)
    return [
        {"id": i, "name": n, "country": c, "referrer_id": r, "seats": s, "plan": plans.get(i)}
        for i, n, c, r, s in CUSTOMERS
    ]


def ticket_rows() -> list[dict]:
    assignees = dict(ASSIGNMENTS)
    return [
        {"id": i, "customer_id": c, "subject": s, "status": st, "priority": p, "legacy_key": k, "body": b, "assignee": assignees.get(i)}
        for i, c, s, st, p, k, b in TICKETS
    ]
