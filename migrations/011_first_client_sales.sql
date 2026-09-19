-- Evidence-backed first-client outreach fields.
-- These fields preserve why a prospect belongs in the queue and where a
-- contact route came from.  They are intentionally separate from activity
-- history: changing a route must not rewrite what was actually sent.

ALTER TABLE organizations ADD COLUMN website_url TEXT;
ALTER TABLE organizations ADD COLUMN fit_tier TEXT
    CHECK (fit_tier IN ('A','B','C','DISQUALIFIED'));
ALTER TABLE organizations ADD COLUMN buying_signal TEXT;
ALTER TABLE organizations ADD COLUMN personalization_fact TEXT;

ALTER TABLE contacts ADD COLUMN title TEXT;
ALTER TABLE contacts ADD COLUMN source_url TEXT;

ALTER TABLE opportunities ADD COLUMN outreach_route TEXT
    CHECK (outreach_route IN ('email','phone','contact_form','linkedin','directory_message','none'));
ALTER TABLE opportunities ADD COLUMN contact_basis TEXT;
ALTER TABLE opportunities ADD COLUMN no_solicit_checked_at TEXT;
ALTER TABLE opportunities ADD COLUMN relevance_reason TEXT;
ALTER TABLE opportunities ADD COLUMN do_not_contact INTEGER NOT NULL DEFAULT 0
    CHECK (do_not_contact IN (0,1));

CREATE INDEX IF NOT EXISTS idx_organizations_fit ON organizations(fit_tier, lifecycle_stage);
