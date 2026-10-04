-- Throwaway end-to-end data for YoDb. Run as the postgres superuser against
-- database yodb_e2e. Idempotent: drops and recreates everything it owns.
CREATE EXTENSION IF NOT EXISTS vector;
DROP SCHEMA IF EXISTS crm, billing, support, helpdesk, bulk CASCADE;
CREATE SCHEMA crm; CREATE SCHEMA billing; CREATE SCHEMA support; CREATE SCHEMA helpdesk; CREATE SCHEMA bulk;

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

-- Semantic search: tickets with a toy 5-dim embedding (keyword counts + bias).
-- The Python ToyEmbedder in run_e2e.py computes exactly the same vector.
CREATE FUNCTION helpdesk.kw(t text, k text) RETURNS int LANGUAGE sql IMMUTABLE AS
  $$ SELECT (length(lower(t)) - length(replace(lower(t), k, ''))) / length(k) $$;
CREATE FUNCTION helpdesk.toy_embed(t text) RETURNS vector(5) LANGUAGE sql IMMUTABLE AS
  $$ SELECT ('[' || helpdesk.kw(t,'price') || ',' || helpdesk.kw(t,'cancel') || ',' ||
             helpdesk.kw(t,'refund') || ',' || helpdesk.kw(t,'bug') || ',1]')::vector(5) $$;
CREATE TABLE helpdesk.tickets (
  ticket_id text PRIMARY KEY, subject text, body text, priority integer, body_embedding vector(5)
);
INSERT INTO helpdesk.tickets (ticket_id, subject, body, priority) VALUES
 ('t01','A01','The price is far too high, we may cancel',                5),
 ('t02','A02','Love the product',                                         2),
 ('t03','A03','Price increase again, thinking to cancel the plan',         4),
 ('t04','A04','App crashed, found a bug',                                  3),
 ('t05','A05','Please refund the price difference',                        5),
 ('t06','A06',NULL,                                                        5),
 ('t07','A07','   ',                                                       5),
 ('t08','A08','We will cancel unless price drops',                         1),
 ('t09','A09','Great support, no complaints',                              4),
 ('t10','A10','Cancel my account, price too steep and a bug too',          5),
 ('t11','A11','Refund requested after bug, no cancel intended',            3),
 ('t12','A12','PRICE and CANCEL in capitals',                              4),
 ('t13','A13','Feature request: dark mode',                                2),
 ('t14','A14','price price price',                                         5),
 ('t15','A15','Considering to cancel, price comparison with rivals',       3),
 ('t16','A16','Billing question',                                          4),
 ('t17','A17','Bug in export',                                             5),
 ('t18','A18','cancel',                                                    5),
 ('t19','A19','Price matching, will not cancel',                           2),
 ('t20','A20','Happy customer',                                            5);
UPDATE helpdesk.tickets SET body_embedding = helpdesk.toy_embed(body) WHERE body IS NOT NULL;
-- The owner of each ticket lives in a second source.
CREATE TABLE helpdesk.owners (ticket_id text PRIMARY KEY, owner text);
INSERT INTO helpdesk.owners VALUES
 ('t01','ann'),('t03','ann'),('t04','bob'),('t05','ann'),('t08','bob'),('t10','ann'),
 ('t12','bob'),('t15','ann'),('t18','ann'),('t19','bob');

-- Optimizer cases: two 200,000-row sources describing the same items.
-- kind has 1,000 distinct values (kind = 'k8' keeps 200 rows); tag has 2
-- (tag = 'hot' keeps 100,000 rows, ten times the 10,000-row scan guard).
CREATE TABLE bulk.items (item_id text PRIMARY KEY, kind text, body text);
INSERT INTO bulk.items SELECT 'i' || g, 'k' || (g % 1000), 'item ' || g FROM generate_series(1, 200000) g;
CREATE TABLE bulk.tags (item_id text PRIMARY KEY, tag text);
INSERT INTO bulk.tags SELECT 'i' || g, CASE WHEN g % 2 = 0 THEN 'hot' ELSE 'cold' END FROM generate_series(1, 200000) g;

-- Least-privilege, read-only role used by YoDb.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'yodb_ro') THEN
    CREATE ROLE yodb_ro LOGIN PASSWORD 'yodb_ro';
  END IF;
END $$;
GRANT USAGE ON SCHEMA crm, billing, support, helpdesk, bulk TO yodb_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA crm, billing, support, helpdesk, bulk TO yodb_ro;
ANALYZE;
