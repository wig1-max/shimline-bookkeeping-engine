-- Accounting firms, and which clients each of their people may see.
--
-- The commercial thesis is that an accountant holds the decisions while
-- Shimline does the bookkeeping. That requires a firm to be a first-class
-- tenant: many clients, many staff, and a hard boundary between one firm and
-- the next. Until now every signed-in staff session could read every
-- bookkeeping run in the database, which was correct for a single-tenant
-- internal console and is a disclosure of one client's books to another firm
-- the moment a second firm exists.
--
-- Three tables rather than one, because two different questions are being
-- asked. `firm_clients` is which clients the *firm* is engaged for -- a
-- commercial fact. `firm_client_assignments` is which of those a particular
-- staff accountant may open -- an access-control fact. Collapsing them would
-- mean adding a client to a firm silently granted it to every junior.
--
-- Shimline's own staff belong to no firm. They are not modelled as a firm with
-- every client attached, because that would make an internal account
-- indistinguishable from a very large customer, and the two must never be
-- confused in an audit trail.

CREATE TABLE IF NOT EXISTS firms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'suspended')),
    -- A suspended firm keeps its rows and loses its access. Deleting a firm
    -- would take its assignment history with it, and the question "who could
    -- see these books in March" has to stay answerable.
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS firm_members (
    firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    firm_role TEXT NOT NULL CHECK (firm_role IN ('principal', 'staff')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (firm_id, user_id),
    -- One firm per person. A contractor working for two firms needs two
    -- logins, which is the honest answer: one session must never be able to
    -- carry one firm's client list into the other firm's view.
    UNIQUE (user_id)
);

CREATE TABLE IF NOT EXISTS firm_clients (
    firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (firm_id, organization_id),
    -- A client belongs to one firm at a time. Two firms holding the same books
    -- is a situation nobody has asked for and one this system should refuse
    -- rather than half-support.
    UNIQUE (organization_id)
);

CREATE TABLE IF NOT EXISTS firm_client_assignments (
    firm_id TEXT NOT NULL REFERENCES firms(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (firm_id, user_id, organization_id)
);

CREATE INDEX IF NOT EXISTS idx_firm_clients_org ON firm_clients(organization_id);
CREATE INDEX IF NOT EXISTS idx_firm_assignments_user
    ON firm_client_assignments(user_id);
