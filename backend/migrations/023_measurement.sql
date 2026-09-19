-- Consent-gated, first-party website measurement. No IPs, raw referrers,
-- query strings, contact details, form values, or filenames belong here.
CREATE TABLE IF NOT EXISTS measurement_events (
    id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    visitor_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    event_name TEXT NOT NULL,
    page_path TEXT NOT NULL,
    referrer_class TEXT NOT NULL,
    utm_source TEXT NOT NULL DEFAULT '',
    utm_medium TEXT NOT NULL DEFAULT '',
    utm_campaign TEXT NOT NULL DEFAULT '',
    utm_term TEXT NOT NULL DEFAULT '',
    utm_content TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_measurement_events_received ON measurement_events(received_at);
CREATE INDEX IF NOT EXISTS idx_measurement_events_funnel ON measurement_events(event_name, received_at);
CREATE INDEX IF NOT EXISTS idx_measurement_events_campaign ON measurement_events(utm_source, utm_medium, utm_campaign, received_at);

-- An order receives attribution only when the browser has opted into the same
-- first-party measurement. Payment and submission data remain authoritative.
CREATE TABLE IF NOT EXISTS measurement_orders (
    order_id TEXT PRIMARY KEY REFERENCES payments(order_id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    paid_at TEXT,
    visitor_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    utm_source TEXT NOT NULL DEFAULT '',
    utm_medium TEXT NOT NULL DEFAULT '',
    utm_campaign TEXT NOT NULL DEFAULT '',
    utm_term TEXT NOT NULL DEFAULT '',
    utm_content TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_measurement_orders_campaign ON measurement_orders(utm_source, utm_medium, utm_campaign, created_at);
