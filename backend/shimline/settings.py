"""Typed application configuration loaded once at process start.

Secrets remain values, never repr-visible strings. Defaults keep local tests and
the public sandbox usable, while production settings still come exclusively
from the service environment.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, PositiveInt, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# The provinces and territories `holidays` can actually build a calendar for.
# Read from the library rather than transcribed, so an upgrade that adds or
# renames a subdivision cannot leave this list quietly wrong.
try:
    from holidays.countries.canada import Canada as _Canada

    CANADIAN_SUBDIVISIONS = frozenset(_Canada.subdivisions)
except Exception:  # pragma: no cover - defensive; holidays is a pinned dependency
    CANADIAN_SUBDIVISIONS = frozenset(
        {"AB", "BC", "MB", "NB", "NL", "NS", "NT", "NU", "ON", "PE", "QC", "SK", "YT"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        case_sensitive=True,
        extra="ignore",
        env_file=None,
    )

    db_path: Path = Field(BASE_DIR / "intake.db", validation_alias="SHIMLINE_DB_PATH")
    uploads_dir: Path = Field(BASE_DIR / "uploads", validation_alias="SHIMLINE_UPLOADS_DIR")

    smtp_host: str = Field("", validation_alias="SMTP_HOST")
    smtp_port: PositiveInt = Field(587, validation_alias="SMTP_PORT")
    smtp_user: str = Field("", validation_alias="SMTP_USER")
    smtp_pass: SecretStr = Field(SecretStr(""), validation_alias="SMTP_PASS")
    notify_to: str = Field("", validation_alias="NOTIFY_TO")
    notify_from: str = Field("", validation_alias="NOTIFY_FROM")
    max_upload_mb: PositiveInt = Field(20, validation_alias="MAX_UPLOAD_MB")

    retention_days_after_close: PositiveInt = Field(
        30, validation_alias="RETENTION_DAYS_AFTER_CLOSE"
    )
    retention_days_unclosed: PositiveInt = Field(
        90, validation_alias="RETENTION_DAYS_UNCLOSED"
    )

    razorpay_key_id: str = Field("", validation_alias="RAZORPAY_KEY_ID")
    razorpay_key_secret: SecretStr = Field(
        SecretStr(""), validation_alias="RAZORPAY_KEY_SECRET"
    )
    razorpay_webhook_secret: SecretStr = Field(
        SecretStr(""), validation_alias="RAZORPAY_WEBHOOK_SECRET"
    )
    price_amount: PositiveInt = Field(19900, validation_alias="PRICE_AMOUNT")
    price_currency: str = Field("CAD", validation_alias="PRICE_CURRENCY")
    upload_token_hours: PositiveInt = Field(72, validation_alias="UPLOAD_TOKEN_HOURS")

    qbo_client_id: str = Field("", validation_alias="QBO_CLIENT_ID")
    qbo_client_secret: SecretStr = Field(
        SecretStr(""), validation_alias="QBO_CLIENT_SECRET"
    )
    qbo_redirect_uri: str = Field(
        "http://localhost:8000/qbo/callback", validation_alias="QBO_REDIRECT_URI"
    )
    qbo_environment: str = Field("sandbox", validation_alias="QBO_ENVIRONMENT")
    qbo_token_key: SecretStr = Field(SecretStr(""), validation_alias="QBO_TOKEN_KEY")
    qbo_authorize_origin: str = Field(
        "https://appcenter.intuit.com", validation_alias="QBO_AUTHORIZE_ORIGIN"
    )

    portal_base_url: str = Field(
        "http://localhost:8000", validation_alias="PORTAL_BASE_URL"
    )
    static_version: str = Field("", validation_alias="SHIMLINE_STATIC_VERSION")

    business_timezone: str = Field(
        "America/Toronto", validation_alias="SHIMLINE_BUSINESS_TZ"
    )
    business_province: str = Field("ON", validation_alias="SHIMLINE_BUSINESS_PROVINCE")

    metrics_enabled: bool = Field(False, validation_alias="SHIMLINE_METRICS_ENABLED")
    metrics_token: SecretStr = Field(
        SecretStr(""), validation_alias="SHIMLINE_METRICS_TOKEN"
    )
    measurement_enabled: bool = Field(
        False, validation_alias="SHIMLINE_MEASUREMENT_ENABLED"
    )
    measurement_retention_days: PositiveInt = Field(
        90, validation_alias="SHIMLINE_MEASUREMENT_RETENTION_DAYS"
    )
    async_jobs_enabled: bool = Field(False, validation_alias="SHIMLINE_ASYNC_JOBS_ENABLED")
    task_db_path: Path = Field(
        BASE_DIR / "shimline_tasks.db", validation_alias="SHIMLINE_TASK_DB_PATH"
    )
    invoice_ocr_backend: Literal["none", "paddle"] = Field(
        "none", validation_alias="SHIMLINE_INVOICE_OCR_BACKEND"
    )

    @field_validator("price_currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha():
            raise ValueError("PRICE_CURRENCY must be a three-letter currency code")
        return value

    @field_validator("qbo_environment")
    @classmethod
    def validate_qbo_environment(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"sandbox", "production"}:
            raise ValueError("QBO_ENVIRONMENT must be sandbox or production")
        return value

    @field_validator("business_province")
    @classmethod
    def normalize_province(cls, value: str) -> str:
        value = value.strip().upper()
        # Shape is not validity. "XX" is two alphabetic characters and passes a
        # length check, then raises NotImplementedError deep inside `holidays`
        # the first time anything computes a business date -- which is the SLA
        # path, the intake path, and the admin queue. Check it against the real
        # subdivision list here, at boot, where a bad value is a startup failure
        # instead of a 500 on a customer's first request.
        if value not in CANADIAN_SUBDIVISIONS:
            raise ValueError(
                f"SHIMLINE_BUSINESS_PROVINCE={value!r} is not a Canadian "
                f"province or territory. Expected one of: "
                f"{', '.join(sorted(CANADIAN_SUBDIVISIONS))}")
        return value

    @property
    def effective_notify_from(self) -> str:
        return self.notify_from or self.smtp_user

    @property
    def normalized_portal_base_url(self) -> str:
        return self.portal_base_url.rstrip("/")


settings = Settings()  # pyright: ignore[reportCallIssue] - values come from environment aliases
