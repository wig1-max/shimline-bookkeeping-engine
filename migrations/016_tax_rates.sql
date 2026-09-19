-- The client's tax rates, named, and classified by which return they belong on.
--
-- `sales_tax.period` reports per tax-rate reference and cannot say "GST 5%",
-- because nothing read the rates. A return line that cannot name its own rate
-- cannot be filed, so this is the first thing the GST/HST lane needs.
--
-- The classification is the part that matters more than the name. **PST and QST
-- are not on a GST/HST return at all**: provincial sales tax is not an input tax
-- credit, and quietly including it would overstate the credit claimed on a
-- return filed with the CRA under the client's name. That is the single most
-- expensive mistake available in this lane, and it is invisible -- the return
-- would look entirely reasonable.
--
-- So every rate is classified, and a rate nobody can classify blocks the return
-- rather than being assumed either way. `classification_source` records whether
-- that came from the tax agency QuickBooks names, from the rate's own name, or
-- from a person deciding. An operator's decision outranks both, and is kept
-- separately so a re-sync cannot silently overwrite it.

CREATE TABLE IF NOT EXISTS bookkeeping_tax_agencies (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    name TEXT NOT NULL,
    sync_token TEXT,
    UNIQUE (organization_id, provider, provider_id)
);

CREATE TABLE IF NOT EXISTS bookkeeping_tax_rates (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    -- Stored as TEXT like every other number in this schema. A rate of 5 means
    -- five percent; it is never a fraction, because QuickBooks does not publish
    -- it as one.
    rate_percent TEXT,
    agency_provider_id TEXT,
    special_tax_type TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    sync_token TEXT,

    -- 'gst_hst'   goes on the GST/HST return
    -- 'provincial' is PST, QST or similar: real tax, not on this return
    -- 'unknown'   blocks the return and names itself
    classification TEXT NOT NULL DEFAULT 'unknown'
        CHECK (classification IN ('gst_hst', 'provincial', 'unknown')),
    classification_source TEXT NOT NULL DEFAULT 'none'
        CHECK (classification_source IN ('agency', 'name', 'operator', 'none')),

    UNIQUE (organization_id, provider, provider_id)
);

-- An operator's decision about a rate, kept apart from the synced row so that
-- re-reading the provider cannot overwrite a judgement a person made. Who
-- decided, and when, because this decision changes a number filed with the CRA.
CREATE TABLE IF NOT EXISTS bookkeeping_tax_rate_overrides (
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    classification TEXT NOT NULL
        CHECK (classification IN ('gst_hst', 'provincial')),
    decided_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    reason TEXT,
    decided_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (organization_id, provider, provider_id)
);

CREATE INDEX IF NOT EXISTS idx_bookkeeping_tax_rates_org
    ON bookkeeping_tax_rates(organization_id, classification);
