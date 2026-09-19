-- How often a client files GST/HST, and when their year ends.
--
-- A filing period is not a calendar quarter. A client's reporting frequency is
-- assigned by the CRA (or elected), their fiscal year end is their own, and an
-- annual filer's quarters run from that year end rather than from January.
-- Assuming Jan-Mar/Apr-Jun would put a return on the wrong period, which is a
-- filing error that looks like an ordinary return.
--
-- `is_individual` exists for one reason and it is worth stating: an annual
-- filer's return is due three months after the fiscal year end -- *unless* the
-- registrant is an individual with a December 31 year end, in which case the
-- return is due June 15 and the payment April 30. Those two dates differ, which
-- is unusual enough that the fact has to be recorded rather than assumed. A
-- client whose status nobody has recorded gets no due date rather than a
-- plausible one.

CREATE TABLE IF NOT EXISTS bookkeeping_gst_filing (
    organization_id TEXT PRIMARY KEY
        REFERENCES organizations(id) ON DELETE CASCADE,
    -- 'monthly', 'quarterly' or 'annual'. Nothing is defaulted: a wrong
    -- frequency produces a return for a period the client does not file.
    frequency TEXT NOT NULL
        CHECK (frequency IN ('monthly', 'quarterly', 'annual')),
    fiscal_year_end_month INTEGER NOT NULL
        CHECK (fiscal_year_end_month BETWEEN 1 AND 12),
    fiscal_year_end_day INTEGER NOT NULL
        CHECK (fiscal_year_end_day BETWEEN 1 AND 31),
    -- Kept because it goes on the return and because its absence is a reason
    -- not to prepare one. Not encrypted: a GST number is printed on the
    -- client's own invoices and is not a secret.
    gst_number TEXT,
    is_individual INTEGER CHECK (is_individual IN (0, 1)),
    registered_from TEXT,
    registered_to TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- A prepared return, kept so that what an accountant saw is what they signed.
-- Figures are stored as text, like all money here. `status` never reaches
-- 'filed' from inside this system: Shimline prepares, an accountant files.
CREATE TABLE IF NOT EXISTS bookkeeping_gst_returns (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    due_at TEXT,
    line_101 TEXT,
    line_105 TEXT,
    line_108 TEXT,
    line_109 TEXT,
    filable INTEGER NOT NULL DEFAULT 0 CHECK (filable IN (0, 1)),
    blocked_json TEXT NOT NULL DEFAULT '[]',
    agreement_json TEXT NOT NULL DEFAULT '{}',
    rates_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'prepared'
        CHECK (status IN ('prepared', 'blocked', 'reviewed', 'filed_by_accountant')),
    prepared_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_by_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
    reviewed_at TEXT,
    UNIQUE (organization_id, period_start, period_end)
);

CREATE INDEX IF NOT EXISTS idx_gst_returns_org
    ON bookkeeping_gst_returns(organization_id, period_end DESC);
