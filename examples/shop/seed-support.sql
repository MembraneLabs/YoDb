CREATE TABLE tickets (ticket_id text PRIMARY KEY, customer_id text, subject text, status text, priority int);
INSERT INTO tickets VALUES
 ('t01','c01','Cannot log in','open',4),        ('t02','c01','Invoice is wrong','closed',2),
 ('t03','c02','App crashes on export','open',5), ('t04','c02','Add dark mode','pending',1),
 ('t05','c03','Reset my password','closed',3),   ('t06','c04','Billing question','open',2),
 ('t07','c04','Slow dashboard','open',3),        ('t08','c05','Data missing after sync','open',5),
 ('t09','c05','Feature request: export to CSV','pending',2), ('t10','c06','Cannot upload files','closed',4),
 ('t11','c07','Two-factor not working','open',5), ('t12','c07','Wrong currency shown','pending',3),
 ('t13','c08','Webhook fails','open',4),         ('t14','c08','Typo in the docs','closed',1),
 ('t15','c09','Cannot cancel my plan','open',3), ('t16','c10','Account locked','open',5),
 ('t17','c10','Update my address','closed',1),   ('t18','c02','Slow search','open',2),
 ('t19','c06','Login loop on mobile','open',4),  ('t20','c12','Welcome and thanks','closed',1);
