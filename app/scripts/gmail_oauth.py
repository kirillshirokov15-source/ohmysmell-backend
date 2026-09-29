"""Deprecated CUSTOMER-only alias; prefer app.scripts.customer_gmail_oauth."""
from app.config.settings import settings
from app.integrations.email.gmail_auth import load_gmail_credentials


def main() -> None:
    load_gmail_credentials(allow_interactive=True)
    print("Gmail OAuth authorization: OK")
    print(f"Token saved: {settings.gmail_token_file}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        from app.integrations.email.health import classify
        print('Gmail OAuth: ' + classify(error) + '; existing token preserved')
        raise SystemExit(1)
