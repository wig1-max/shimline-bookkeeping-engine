-- A company already in the CRM can be corrected to a fuller, more accurate
-- name by later research ("Granstone" -> "Granstone Renovations").
-- `normalize_company` sees those as two different companies, so re-importing
-- the corrected research created a second organization rather than merging
-- into the first.  That failure is silent: the duplicate looks exactly like a
-- new lead, one copy holds the contacts and the other holds none, and both
-- look legitimate.
--
-- An alias is the record that two normalized names are the same company.
-- Renames write one automatically, and an operator resolving an import
-- conflict writes one by hand.  Lookup consults it, so the correction only
-- has to be made once.

CREATE TABLE IF NOT EXISTS organization_aliases (
    normalized_name TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    alias_name TEXT NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('rename','import_link')),
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_organization_aliases_org
    ON organization_aliases(organization_id);

-- Which existing organization a conflict row is proposed to be the same as.
-- Kept on the row rather than in the payload: it is a fact about the import
-- decision, not a field that would ever be written to the organization.
ALTER TABLE import_rows ADD COLUMN candidate_organization_id TEXT
    REFERENCES organizations(id) ON DELETE SET NULL;
