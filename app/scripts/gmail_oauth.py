from app.config.settings import settings
from app.integrations.email.gmail_auth import load_gmail_credentials


def main() -> None:
    load_gmail_credentials(allow_interactive=True)
    print("Gmail OAuth authorization: OK")
    print(f"Token saved: {settings.gmail_token_file}")


if __name__ == "__main__":
    main()
