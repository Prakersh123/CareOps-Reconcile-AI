-- Synthetic billing system for CareOps Reconcile AI (PostgreSQL).
-- Loaded into the billing database container on first start (/docker-entrypoint-initdb.d).
-- I104 is the replacement for the cancelled I102; L102 deliberately has no invoice.
-- Messy on purpose, like real exports: L105 is billed twice (I105, I106); I107 was cancelled and never
-- replaced; I108 is unpaid; payment statuses are not consistently cased or trimmed ('Paid', 'unpaid ');
-- and I109 belongs to lab order L108 but was keyed as 'L-108', so it never joins to the lab system.
CREATE TABLE IF NOT EXISTS billing_records (
    invoice_id     varchar(50),
    patient_id     varchar(50),
    order_id       varchar(50),
    invoice_status varchar(30),
    payment_status varchar(30)
);

INSERT INTO billing_records (invoice_id, patient_id, order_id, invoice_status, payment_status) VALUES
    ('I101', 'P101', 'L101', 'ISSUED',    'PAID'),
    ('I102', 'P103', 'L103', 'CANCELLED', 'UNPAID'),
    ('I103', 'P104', 'L104', 'ISSUED',    'PAID'),
    ('I104', 'P103', 'L103', 'ISSUED',    'UNPAID'),
    ('I105', 'P105', 'L105', 'ISSUED',    'PAID'),
    ('I106', 'P105', 'L105', 'ISSUED',    'Paid'),
    ('I107', 'P106', 'L106', 'CANCELLED', 'UNPAID'),
    ('I108', 'P107', 'L107', 'ISSUED',    'unpaid '),
    ('I109', 'P108', 'L-108', 'ISSUED',   'PAID');
