import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    def validate_runtime(self, process="backend"):
        if process not in {"backend", "manager", "client", "email"}:
            raise ValueError("Unknown process role")
        if not self.database_url:
            raise ValueError("DATABASE_URL is required")
        if self.environment == "staging" and self.external_writes_enabled:
            raise ValueError("External writes cannot be enabled in staging")
        if self.environment == "production":
            if process == "backend" and len(self.internal_api_token) < 32:
                raise ValueError("Production requires a strong INTERNAL_API_TOKEN")
            if self.debug_endpoints_enabled:
                raise ValueError("Production debug endpoints must remain disabled")
            if process == "backend" and (not self.cors_origins or any(not origin.startswith("https://") or "localhost" in origin
                                            or "REPLACE" in origin for origin in self.cors_origins)):
                raise ValueError("Production requires explicit final HTTPS CORS origins")
        if self.external_writes_enabled and not self.moysklad_organization_id:
            raise ValueError("External writes require MOYSKLAD_ORGANIZATION_ID")

    app_name: str = os.getenv("APP_NAME", "OhMySmell CRM")
    database_url: str = os.getenv("DATABASE_URL", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    moysklad_organization_id: str = os.getenv(
        "MOYSKLAD_ORGANIZATION_ID",
        "",
    )
    def __init__(self) -> None:
        self.buying_shared_password = os.getenv("BUYING_SHARED_PASSWORD", "")
        self.buying_session_secret = os.getenv("BUYING_SESSION_SECRET", "")
        self.supplier_email_send_enabled = os.getenv("SUPPLIER_EMAIL_SEND_ENABLED", "false").lower() == "true"
        self.environment = os.getenv("APP_ENV", "development")
        if self.environment not in {"development", "staging", "production"}:
            raise ValueError("APP_ENV must be development, staging or production")
        self.cors_origins = [v.strip() for v in os.getenv(
            "CORS_ORIGINS", "http://localhost:5500,http://127.0.0.1:5500"
        ).split(",") if v.strip()]
        if "*" in self.cors_origins:
            raise ValueError("Explicit CORS origins are required")
        self.warehouse_ids = tuple(dict.fromkeys(v.strip() for v in os.getenv(
            "MOYSKLAD_WAREHOUSE_IDS", "").split(",") if v.strip()))
        self.public_checkout_enabled = os.getenv("PUBLIC_CHECKOUT_ENABLED", "false").lower() == "true"
        self.delivery_cdek_client_id = os.getenv("CDEK_CLIENT_ID", "")
        self.delivery_cdek_client_secret = os.getenv("CDEK_CLIENT_SECRET", "")
        self.delivery_yandex_token = os.getenv("YANDEX_DELIVERY_TOKEN", "")
        self.moysklad_wholesale_price_type = os.getenv(
            "MOYSKLAD_WHOLESALE_PRICE_TYPE",
            "Цена продажи",
        )
        self.moysklad_retail_price_type = os.getenv(
            "MOYSKLAD_RETAIL_PRICE_TYPE",
            "",
        )
    gmail_credentials_file: str = os.getenv("GMAIL_CREDENTIALS_FILE", "")
    gmail_token_file: str = os.getenv("GMAIL_TOKEN_FILE", "")
    gmail_user_id: str = os.getenv("GMAIL_USER_ID", "me")
    gmail_initial_query: str = os.getenv(
        "GMAIL_INITIAL_QUERY", "label:inbox is:unread"
    )
    email_poll_interval: int = int(os.getenv("EMAIL_POLL_INTERVAL", "60"))
    internal_api_token: str = os.getenv("INTERNAL_API_TOKEN", "")
    debug_endpoints_enabled: bool = os.getenv(
        "DEBUG_ENDPOINTS_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}
    external_writes_enabled: bool = os.getenv(
        "EXTERNAL_WRITES_ENABLED", "false"
    ).lower() in {"1", "true", "yes", "on"}
    moysklad_catalog_cache_ttl_seconds: float = float(
        os.getenv("MOYSKLAD_CATALOG_CACHE_TTL_SECONDS", "60")
    )


settings = Settings()
