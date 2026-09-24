from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    api_prefix: str = "/api/v1"
    cors_origins: str = "http://localhost:8000"

    database_url: str = "postgresql+psycopg://gradorra:gradorra@localhost:5435/gradorra"

    jwt_secret: str = "change-me"
    form_token_ttl_minutes: int = 15

    otp_channel: str = "console"
    otp_length: int = 6
    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    otp_max_resends: int = 10  # per mobile per hour, login and registration combined

    whatsapp_provider: str = "console"
    whatsapp_phone_number_id: str = ""
    whatsapp_business_account_id: str = ""
    whatsapp_token: str = ""
    whatsapp_api_version: str = "v25.0"
    whatsapp_template_otp: str = "gpet_otp"
    whatsapp_template_ack: str = "gpet_acknowledgement"
    whatsapp_template_language: str = "en"
    # text | template  -- template needs an approved template on a real WABA
    whatsapp_ack_channel: str = "template"
    # Meta rejects more than one message per 6 seconds to the same user.
    whatsapp_min_seconds_between_messages: int = 6
    # Messaging limit: unique recipients per rolling 24h. 250 on a fresh number,
    # 2,000 after business verification, then 10K / 100K as quality allows.
    whatsapp_daily_unique_recipients: int = 250
    # Some authentication templates want the code repeated in a button component.
    # Left off by default; the docs say the button is fixed at template creation.
    whatsapp_auth_template_button: bool = True

    # ---- email ----
    # console | smtp
    email_provider: str = "console"
    email_enabled: bool = False
    email_from: str = "info@gradorra.in"
    email_from_name: str = "GPET 2026"
    email_reply_to: str = "info@gradorra.in"
    email_attach_receipt: bool = True
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False

    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    ack_exam_code: str = "GPET26"
    ack_state_prefix: str = "UP"
    ack_digits: int = 5

    current_phase: str = "PRE_LAUNCH"
    fee_prelaunch_paise: int = 9900
    fee_postlaunch_paise: int = 49900
    fee_postlaunch_discounted_paise: int = 29900

    registration_expiry_hours: int = 48

    # abuse control on the public endpoints
    rate_limit_registration: int = 5
    rate_limit_registration_window_seconds: int = 600
    rate_limit_otp: int = 10
    rate_limit_otp_window_seconds: int = 600
    rate_limit_global: int = 100
    rate_limit_global_window_seconds: int = 60
    # none | turnstile | recaptcha
    bot_check_provider: str = "none"
    bot_check_secret: str = ""
    bot_check_required: bool = False

    # background settler for payments the browser never reported
    # create tables and seed districts on boot -- for a fresh deploy with no migrations
    auto_init_db: bool = False
    sync_enabled: bool = True
    sync_interval_minutes: int = 10
    sync_min_age_minutes: int = 5
    sync_batch_size: int = 50

    @field_validator("database_url")
    @classmethod
    def name_the_driver(cls, v: str) -> str:
        """Render, Heroku and RDS all hand out postgres:// or postgresql:// URLs.
        SQLAlchemy needs the driver spelled out, so add it rather than making
        every deployment remember to."""
        if v.startswith("postgres://"):
            return "postgresql+psycopg://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            return "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def razorpay_configured(self) -> bool:
        return bool(self.razorpay_key_id and self.razorpay_key_secret)


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
