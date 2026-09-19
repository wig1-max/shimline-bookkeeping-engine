CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS roles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id TEXT NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id)
);

INSERT OR IGNORE INTO roles(id, name) VALUES
    ('owner', 'Owner'),
    ('operator', 'Operator'),
    ('reviewer', 'Reviewer'),
    ('viewer', 'Viewer');

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    absolute_expires_at TEXT NOT NULL,
    ip_hash TEXT,
    user_agent TEXT
);

CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS login_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    email TEXT,
    ip_hash TEXT,
    succeeded INTEGER NOT NULL DEFAULT 0 CHECK (succeeded IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_time ON login_attempts(ip_hash, attempted_at);

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    lifecycle_stage TEXT NOT NULL DEFAULT 'prospect'
        CHECK (lifecycle_stage IN ('prospect','qualified','proposal','onboarding','active','paused','offboarded','lost')),
    city TEXT,
    category TEXT,
    specialty TEXT,
    fit_notes TEXT,
    source_url TEXT,
    owner_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    next_action TEXT,
    next_action_due_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_organizations_stage ON organizations(lifecycle_stage);
CREATE INDEX IF NOT EXISTS idx_organizations_next_action ON organizations(next_action_due_at);

CREATE TABLE IF NOT EXISTS contacts (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    name TEXT,
    email TEXT COLLATE NOCASE,
    phone TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_contacts_org ON contacts(organization_id);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);

CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    stage TEXT NOT NULL DEFAULT 'new'
        CHECK (stage IN ('new','contacted','replied','qualified','proposal_sent','won','lost')),
    service_type TEXT NOT NULL DEFAULT 'cash_leak_review',
    amount INTEGER,
    currency TEXT DEFAULT 'CAD',
    source TEXT,
    sequence_step TEXT,
    last_touch_at TEXT,
    next_follow_up_at TEXT,
    replied INTEGER NOT NULL DEFAULT 0 CHECK (replied IN (0, 1)),
    sample_sent INTEGER NOT NULL DEFAULT 0 CHECK (sample_sent IN (0, 1)),
    review_sold INTEGER NOT NULL DEFAULT 0 CHECK (review_sold IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_opportunities_stage ON opportunities(stage);
CREATE INDEX IF NOT EXISTS idx_opportunities_org ON opportunities(organization_id);

CREATE TABLE IF NOT EXISTS engagements (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE RESTRICT,
    submission_id TEXT UNIQUE,
    engagement_type TEXT NOT NULL DEFAULT 'cash_leak_review',
    title TEXT NOT NULL,
    accounting_period TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_client'
        CHECK (status IN ('draft','awaiting_payment','awaiting_client','ready','in_progress','internal_review','delivered','closed')),
    priority_reason TEXT,
    assigned_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    ready_at TEXT,
    due_at TEXT,
    delivered_at TEXT,
    closed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_engagements_org ON engagements(organization_id);
CREATE INDEX IF NOT EXISTS idx_engagements_queue ON engagements(status, due_at);

CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    engagement_id TEXT NOT NULL REFERENCES engagements(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'todo'
        CHECK (status IN ('todo','in_progress','waiting_client','review','done','skipped')),
    position INTEGER NOT NULL DEFAULT 0,
    due_at TEXT,
    completed_at TEXT,
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_work_items_engagement ON work_items(engagement_id, position);
CREATE INDEX IF NOT EXISTS idx_work_items_queue ON work_items(status, due_at);

CREATE TABLE IF NOT EXISTS activities (
    id TEXT PRIMARY KEY,
    organization_id TEXT REFERENCES organizations(id) ON DELETE CASCADE,
    engagement_id TEXT REFERENCES engagements(id) ON DELETE CASCADE,
    actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_activities_org_time ON activities(organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_activities_engagement_time ON activities(engagement_id, created_at DESC);

CREATE TABLE IF NOT EXISTS audit_events (
    id TEXT PRIMARY KEY,
    actor_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    summary TEXT,
    request_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id, created_at DESC);

CREATE TABLE IF NOT EXISTS import_batches (
    id TEXT PRIMARY KEY,
    source_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'preview' CHECK (status IN ('preview','applied','failed')),
    summary_json TEXT NOT NULL,
    created_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_at TEXT
);

CREATE TABLE IF NOT EXISTS import_rows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    row_key TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('create','merge','skip','conflict')),
    payload_json TEXT NOT NULL,
    UNIQUE(batch_id, row_key)
);
