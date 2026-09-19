-- A regular-method GST/HST return and a Quick Method return do not use the
-- same arithmetic. Until the client's method is recorded, producing the
-- regular-method figures is a confident answer to an unanswered question.
-- Existing arrangements intentionally remain NULL so they fail closed and an
-- operator has to record the fact rather than inherit a guessed default.

ALTER TABLE bookkeeping_gst_filing
ADD COLUMN calculation_method TEXT
    CHECK (calculation_method IN ('regular', 'quick'));
