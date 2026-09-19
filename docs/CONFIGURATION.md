# Configuration

Every setting is an environment variable. Copy [`.env.example`](../.env.example)
to `.env` and change the values marked `replace-me`. Nothing has a production
default: out of the box the service runs against local SQLite files and the
QuickBooks **sandbox**.

| Group | Variables | Notes |
| --- | --- | --- |
| Storage | `SHIMLINE_DB_PATH`, `SHIMLINE_UPLOADS_DIR`, `SHIMLINE_TASK_DB_PATH`, `MAX_UPLOAD_MB` | SQLite database, uploaded documents, background task queue |
| Retention | `RETENTION_DAYS_AFTER_CLOSE`, `RETENTION_DAYS_UNCLOSED` | Enforced by `backend/purge.py`; covered by `test_retention.py` |
| Security | `SHIMLINE_SECRET_KEY`, `ADMIN_COOKIE_SECURE` | The secret key derives per-purpose keys for TOTP seeds and session material. Rotating it invalidates every enrolled second factor |
| QuickBooks Online | `QBO_CLIENT_ID`, `QBO_CLIENT_SECRET`, `QBO_REDIRECT_URI`, `QBO_ENVIRONMENT`, `QBO_TOKEN_KEY`, `QBO_AUTHORIZE_ORIGIN` | `QBO_TOKEN_KEY` encrypts OAuth tokens and realm IDs at rest. Rotating it forces every connection to re-authorize. Writes are refused unless `QBO_ENVIRONMENT=sandbox` |
| Calendar | `SHIMLINE_BUSINESS_TZ`, `SHIMLINE_BUSINESS_PROVINCE` | Drives filing-period and statutory-holiday calculations |
| Background jobs | `SHIMLINE_ASYNC_JOBS_ENABLED`, `SHIMLINE_INVOICE_OCR_BACKEND` | Huey worker for OCR and long pulls |
| Observability | `SHIMLINE_METRICS_ENABLED`, `SHIMLINE_METRICS_TOKEN`, `SHIMLINE_RELEASE_ID` | Prometheus metrics; loopback-only unless a bearer token is set |
| Measurement | `SHIMLINE_MEASUREMENT_ENABLED`, `SHIMLINE_MEASUREMENT_RETENTION_DAYS` | Consent-gated first-party analytics, off by default |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `NOTIFY_TO`, `NOTIFY_FROM` | Operator notifications only |
| Payments | `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET`, `PRICE_AMOUNT`, `PRICE_CURRENCY`, `UPLOAD_TOKEN_HOURS` | Optional paid-intake flow |
| Portal | `PORTAL_BASE_URL`, `SHIMLINE_STATIC_VERSION` | Base URL used in signed, expiring client links |

## Running behind a reverse proxy

The app is a standard ASGI service (`uvicorn app:app` from `backend/`). Put it
behind any TLS-terminating proxy, set `ADMIN_COOKIE_SECURE=1`, and keep
`/metrics` private. The OAuth callback strips query strings from access logs so
authorization codes and realm IDs are never written to disk.
