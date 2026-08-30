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


settings = Settings()
