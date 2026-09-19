-- The professional of record, as disclosed, kept as it was disclosed.
--
-- Shimline is the single brand and the single interface a client deals with.
-- The work is done by a named professional at a named firm, and the client is
-- told who. That is the whole difference between a marketplace that is honest
-- about its supply and one that pretends the work has no author.
--
-- 020 gave the engagement its `firm_id`, and `assigned_user_id` was already
-- there. Those two answer "who is doing this now", which is an operational
-- question whose answer changes: staff are reassigned, firms are renamed, people
-- leave and their user row is deleted -- and `assigned_user_id` is
-- `ON DELETE SET NULL`, so today the answer to "who did the work in March"
-- disappears with them.
--
-- A disclosure cannot work that way. What was said to the client in March has
-- to still read as what was said to the client in March, so the names are
-- snapshotted at the moment of disclosure rather than joined at read time. This
-- is the same reason an invoice keeps the address it was sent to instead of
-- following the customer record.
--
-- Three columns rather than two, because "nobody has been named yet" and "the
-- client was told on this date" are different states and only the second may be
-- shown as a disclosure.

ALTER TABLE engagements ADD COLUMN professional_name TEXT;
ALTER TABLE engagements ADD COLUMN professional_firm_name TEXT;
ALTER TABLE engagements ADD COLUMN professional_disclosed_at TEXT;
