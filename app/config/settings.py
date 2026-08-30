import os
from dotenv import load_dotenv

load_dotenv()


class Settings:
    app_name: str = os.getenv("APP_NAME", "OhMySmell CRM")
    database_url: str = os.getenv("DATABASE_URL", "")
    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    moysklad_organization_id: str = os.getenv(
        "MOYSKLAD_ORGANIZATION_ID",
        "ef8e60b2-c856-11f0-0a80-00b00020eed1",
    )
    moysklad_wholesale_price_type: str = os.getenv(
        "MOYSKLAD_WHOLESALE_PRICE_TYPE",
        "Цена продажи",
    )
    moysklad_retail_price_type: str = os.getenv(
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


settings = Settings()
