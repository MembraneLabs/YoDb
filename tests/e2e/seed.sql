-- Throwaway end-to-end data for YoDb. Run as the postgres superuser against
-- database yodb_e2e. Idempotent: drops and recreates everything it owns.
DROP SCHEMA IF EXISTS crm, billing, support CASCADE;
CREATE SCHEMA crm; CREATE SCHEMA billing; CREATE SCHEMA support;

-- CRM: identity source for `customer`. 12 customers with NULLs, a duplicate
-- name (c01/c12 -> tie-break by id), and mixed case ("Acme" vs "acme").
CREATE TABLE crm.accounts (
  account_id     text PRIMARY KEY,
  company_name   text,
  account_status text,
  seats          integer,
  signup_at      timestamp,
  is_vip         boolean,
  country        text
);
INSERT INTO crm.accounts VALUES
 ('c01','Acme Corporation','active',  50,'2024-01-10 09:00',true ,'US'),
 ('c02','acme labs',       'active',   5,'2024-03-02 12:30',false,'US'),
 ('c03','Bravo Inc',       'inactive',20,'2023-11-30 08:15',false,'UK'),
 ('c04','Charlie Co',      'active',  NULL,'2025-01-15 17:45',false,'DE'),
 ('c05','Delta Dynamics',  'pending',100,'2022-06-01 00:00',true ,'US'),
 ('c06','Echo Systems',    'active',   7,NULL,                false,'FR'),
 ('c07','Foxtrot Ltd',     'inactive',12,'2024-08-19 10:10',false,'UK'),
 ('c08','Golf & Co',       'active',  80,'2025-05-05 05:05',true ,'DE'),
 ('c09',NULL,              'active',   1,'2025-06-06 06:06',false,NULL),
 ('c10','Hotel Holdings',  'pending', 33,'2023-02-14 14:14',false,'US'),
 ('c11','India Imports',   'active',   3,'2025-09-01 11:00',false,'IN'),
 ('c12','Acme Corporation','active',   9,'2024-12-12 12:12',false,'US');

-- Billing: contributor. No rows for c04, c09, c12; orphan c20; NULL plan (c06);
-- NULL mrr (c03).
CREATE TABLE billing.customers (
  customer_id text PRIMARY KEY,
  plan        text,
  mrr         double precision,
  auto_renew  boolean
);
INSERT INTO billing.customers VALUES
 ('c01','enterprise',5000, true ),
 ('c02','basic',       49, true ),
 ('c03','basic',     NULL, false),
 ('c05','enterprise',9000, true ),
 ('c06',NULL,           0, false),
 ('c07','pro',        300, false),
 ('c08','enterprise',7000, true ),
 ('c10','pro',        250, true ),
 ('c11','basic',       25, false),
 ('c20','enterprise',1000, true );

-- Support: second contributor (partial coverage, orphan c30, NULL tier on c08).
CREATE TABLE support.tickets_summary (
  customer_id  text PRIMARY KEY,
  tier         text,
  open_tickets integer
);
INSERT INTO support.tickets_summary VALUES
 ('c01','gold',  2), ('c03','silver',0), ('c05','gold',  5),
 ('c08',NULL,    1), ('c11','bronze',0), ('c30','gold',  9);

-- Deliberately broken contributor: c01 appears twice (no unique constraint).
CREATE TABLE billing.dup_accounts (customer_id text, plan text);
INSERT INTO billing.dup_accounts VALUES ('c01','a'), ('c01','b'), ('c02','basic');

-- 10,500 rows: more than the planner's 10,000-row scan guard.
CREATE TABLE crm.events (event_id text PRIMARY KEY, label text);
INSERT INTO crm.events SELECT 'e' || g, 'event-' || g FROM generate_series(1, 10500) g;

-- Least-privilege, read-only role used by YoDb.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yodb_ro') THEN
    CREATE ROLE yodb_ro LOGIN PASSWORD 'yodb_ro';
  END IF;
END $$;
GRANT USAGE ON SCHEMA crm, billing, support TO yodb_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA crm, billing, support TO yodb_ro;
ANALYZE;
