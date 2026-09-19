-- Sales tax as QuickBooks actually states it.
--
-- Until now every persisted line carried tax_amount '0.00', hard-coded. Not
-- unknown -- zero. Anything built on those tables would have computed a GST/HST
-- return of nothing and looked entirely correct doing it, which is the exact
-- shape of failure this codebase refuses everywhere else.
--
-- The fix is not to apportion the document's tax across its lines. QuickBooks
-- does not state tax per line; it states it per *tax rate*, in
-- TxnTaxDetail.TaxLine, together with the net amount that rate applied to.
-- Splitting that across lines would be inventing a number, and a GST return is
-- filed with the CRA under the client's name.
--
-- So the grain here is the grain QuickBooks publishes: a total on the
-- transaction, and one row per rate. That is also exactly the grain a GST/HST
-- return needs -- tax collected and tax paid, per rate, over a period, with the
-- taxable base each was charged on.

ALTER TABLE bookkeeping_transactions ADD COLUMN tax_total TEXT;

-- Whether the tax figure on a line was stated by the provider or is simply not
-- known. The column it qualifies is NOT NULL DEFAULT '0', so without this a
-- reader cannot tell a genuine zero-rated line from one nobody ever filled in.
ALTER TABLE bookkeeping_transaction_lines
    ADD COLUMN tax_amount_source TEXT NOT NULL DEFAULT 'unknown';

CREATE TABLE IF NOT EXISTS bookkeeping_transaction_taxes (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL
        REFERENCES bookkeeping_transactions(id) ON DELETE CASCADE,
    tax_rate_ref TEXT,
    rate_percent TEXT,
    -- The amount that rate was charged on. A return needs the base as much as
    -- the tax: the CRA asks for both, and one without the other cannot be
    -- checked.
    net_amount_taxable TEXT,
    tax_amount TEXT NOT NULL,
    UNIQUE (transaction_id, tax_rate_ref)
);

CREATE INDEX IF NOT EXISTS idx_bookkeeping_txn_taxes_txn
    ON bookkeeping_transaction_taxes(transaction_id);
