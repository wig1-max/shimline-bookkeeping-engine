-- Reporting projection support.
--
-- The client-facing Cash-Leak Review reports vendor and material price
-- movement. That needs the per-unit price a line was billed at, which the
-- canonical model discarded: it kept only the extended amount. Two columns,
-- nullable, because most lines are account-based and carry no unit price at
-- all -- an absent unit price must stay absent rather than default to zero.
ALTER TABLE bookkeeping_transaction_lines ADD COLUMN unit_price TEXT;
ALTER TABLE bookkeeping_transaction_lines ADD COLUMN quantity TEXT;
