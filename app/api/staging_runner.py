"""Local API bound explicitly to verified staging; no deployment mutation."""
import os
from pathlib import Path
import secrets
from dotenv import load_dotenv
from app.bot.staging_runner import validate_staging_config, activate_staging_config
from app.config.settings import settings


def configure():
    load_dotenv()
    config = validate_staging_config(dict(os.environ))
    activate_staging_config(config)
    settings.environment = "staging"
    token_file = Path(".staging-artifacts/internal-api-token.txt")
    token_file.parent.mkdir(exist_ok=True)
    if not token_file.exists():
        token_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
    settings.internal_api_token = os.getenv("STAGING_INTERNAL_API_TOKEN") or token_file.read_text(encoding="utf-8").strip()
    settings.public_checkout_enabled = True
    return config


def main():
    configure()
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001, log_level="info")


if __name__ == "__main__":
    main()
