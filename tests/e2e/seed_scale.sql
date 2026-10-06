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
GRANT USAGE ON SCHEMA scale TO yodb_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA scale TO yodb_ro;
ANALYZE;
