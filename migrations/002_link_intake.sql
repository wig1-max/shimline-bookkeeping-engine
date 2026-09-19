ALTER TABLE submissions ADD COLUMN organization_id TEXT;
ALTER TABLE submissions ADD COLUMN engagement_id TEXT;

CREATE INDEX IF NOT EXISTS idx_submissions_organization ON submissions(organization_id);
CREATE INDEX IF NOT EXISTS idx_submissions_engagement ON submissions(engagement_id);
