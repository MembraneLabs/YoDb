-- c11 and c12 have no subscription row yet, so their plan is NULL
CREATE TABLE subscriptions (customer_id text PRIMARY KEY, plan text);
INSERT INTO subscriptions VALUES
 ('c01','pro'), ('c02','team'), ('c03','free'), ('c04','pro'), ('c05','team'),
 ('c06','free'), ('c07','pro'), ('c08','team'), ('c09','free'), ('c10','pro');
