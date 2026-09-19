-- The client relationship moves from the firm to the engagement.
--
-- 015 gave `firm_clients` a `UNIQUE (organization_id)` and said two firms
-- holding one set of books was "a situation nobody has asked for". That was
-- true of an operating layer sold to one firm at a time. It is not true of the
-- arrangement most Canadian small businesses actually have, which is two
-- providers at once: a bookkeeper monthly and a CPA at year end. Those are not
-- competitors bidding for the same work; they are two standing relationships
-- with different scopes.
--
-- Two changes, and deliberately no third.
--
-- 1. An engagement records which firm is doing the work. `engagements` already
--    carried the client, the type, the lifecycle and the assigned person, and
--    it already allowed many per client -- there is only an index on
--    `organization_id`, never a unique. The firm was the one missing column.
--
-- 2. `firm_clients` stops being unique per client. SQLite cannot drop a
--    constraint, so the table is rebuilt. This is written as a copy-migrate
--    rather than 009's drop-and-recreate because it must be correct whether or
--    not a firm has been onboarded in production; where the table is empty the
--    two are the same thing. Nothing references `firm_clients` by foreign key,
--    so `PRAGMA foreign_keys = ON` has nothing to cascade. The composite
--    primary key stays, so one firm still cannot hold one client twice.
--
-- What deliberately does NOT change is `tenancy._firm_organizations`. Access
-- still resolves through `firm_clients`, one firm at a time, exactly as it does
-- today. Deriving scope from engagement state would make the security boundary
-- depend on an eight-state lifecycle, and which of those states may open a
-- client's books is a decision that wants a real arrangement in front of it.
-- This migration is behaviour-neutral on purpose: it is taken now only because
-- rebuilding an empty table is cheap and rebuilding a populated one is not.

ALTER TABLE engagements
ADD COLUMN firm_id TEXT REFERENCES firms(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_engagements_firm ON engagements(firm_id);

CREATE TABLE firm_clients_rebuilt (
    firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    -- One firm cannot hold one client twice. More than one firm can hold the
    -- same client, which is the whole point of the rebuild.
    PRIMARY KEY (firm_id, organization_id)
);

INSERT INTO firm_clients_rebuilt(firm_id, organization_id, created_at)
SELECT firm_id, organization_id, created_at FROM firm_clients;

DROP TABLE firm_clients;

ALTER TABLE firm_clients_rebuilt RENAME TO firm_clients;

CREATE INDEX IF NOT EXISTS idx_firm_clients_org ON firm_clients(organization_id);
