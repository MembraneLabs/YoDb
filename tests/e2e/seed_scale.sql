-- Scale data for run_robustness.py: a million-row table and a half-million-row contributor.
-- Run after seed.sql, as the postgres superuser against yodb_e2e.
DROP SCHEMA IF EXISTS scale CASCADE;
CREATE SCHEMA scale;
SELECT setseed(0.11);
CREATE TABLE scale.items (
  item_id    text PRIMARY KEY,
  kind       text,
  amount     double precision,
  created_at timestamp,
  flag       boolean,
  note       text
);
INSERT INTO scale.items
SELECT 'x' || lpad(g::text, 7, '0'),
       'k' || (g % 2000),
       round((random() * 1000)::numeric, 2)::double precision,
       timestamp '2020-01-01' + (g % 2000) * interval '1 day',
       g % 7 = 0,
       'note ' || g
FROM generate_series(1, 1000000) g;
CREATE TABLE scale.attrs (item_id text PRIMARY KEY, segment text, score integer, code text);
INSERT INTO scale.attrs
SELECT 'x' || lpad(g::text, 7, '0'), 's' || (g % 50), g % 100, 'c' || (g % 1000)
FROM generate_series(1, 1000000, 2) g;
-- Joins: 50,000 buyers, 300,000 purchases (skewed towards low buyer ids, 1% with no buyer, 1% for buyers that
-- do not exist), a score for 60% of buyers and a carrier for half of the purchases.
SELECT setseed(0.23);
CREATE TABLE scale.buyers (buyer_id text PRIMARY KEY, name text, region text, tier text, joined_at timestamp);
INSERT INTO scale.buyers
SELECT 'b' || lpad(g::text, 6, '0'), 'buyer ' || g, 'r' || lpad((g % 20)::text, 2, '0'),
       (ARRAY['free','basic','pro','team','enterprise'])[1 + (g % 5)], timestamp '2021-01-01' + (g % 1500) * interval '1 day'
FROM generate_series(1, 50000) g;
CREATE TABLE scale.buyer_scores (buyer_id text PRIMARY KEY, score integer);
INSERT INTO scale.buyer_scores SELECT buyer_id, (random() * 100)::int FROM scale.buyers WHERE random() < 0.6;
CREATE TABLE scale.purchases (purchase_id text PRIMARY KEY, buyer_id text, amount double precision, status text, purchased_at timestamp);
INSERT INTO scale.purchases
SELECT 'p' || lpad(g::text, 7, '0'),
       CASE WHEN random() < 0.01 THEN NULL
            WHEN random() < 0.01 THEN 'b9' || lpad(g::text, 5, '0')
            ELSE 'b' || lpad((1 + floor(power(random(), 2) * 50000))::int::text, 6, '0') END,
       round((random() * 1000)::numeric, 2)::double precision,
       (ARRAY['paid','shipped','refunded','failed'])[1 + floor(power(random(), 1.5) * 4)::int],
       timestamp '2022-01-01' + random() * 1000 * interval '1 day'
FROM generate_series(1, 300000) g;
CREATE TABLE scale.purchase_shipping (purchase_id text PRIMARY KEY, carrier text);
INSERT INTO scale.purchase_shipping
SELECT purchase_id, (ARRAY['dhl','ups','fedex'])[1 + floor(random() * 3)::int] FROM scale.purchases WHERE random() < 0.5;
-- Pre-joined copies for the test oracle only (not in the catalog): the same facts, so a SQL join is fast.
CREATE TABLE scale.buyers_flat AS
  SELECT b.*, s.score FROM scale.buyers b LEFT JOIN scale.buyer_scores s USING (buyer_id);
ALTER TABLE scale.buyers_flat ADD PRIMARY KEY (buyer_id);
CREATE TABLE scale.purchases_flat AS
  SELECT p.*, ps.carrier FROM scale.purchases p LEFT JOIN scale.purchase_shipping ps USING (purchase_id);
ALTER TABLE scale.purchases_flat ADD PRIMARY KEY (purchase_id);
CREATE INDEX purchases_flat_buyer ON scale.purchases_flat (buyer_id);
GRANT USAGE ON SCHEMA scale TO yodb_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA scale TO yodb_ro;
ANALYZE;
