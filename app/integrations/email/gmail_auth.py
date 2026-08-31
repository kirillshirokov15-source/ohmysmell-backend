import os
from pathlib import Path

from app.config.settings import settings


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = (GMAIL_READONLY_SCOPE,)


def load_gmail_credentials(*, allow_interactive: bool = False):
    """Load/refresh Gmail credentials, optionally running desktop OAuth once."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = _configured_path(settings.gmail_token_file, "GMAIL_TOKEN_FILE")

    credentials = None
    if token_path.exists():
        credentials = Credentials.from_authorized_user_file(
            str(token_path), GMAIL_SCOPES
        )

    changed = False
    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        changed = True

    if not credentials or not credentials.valid:
        if not allow_interactive:
            raise RuntimeError(
                "Gmail authorization is required; run "
                "'.\\.venv\\Scripts\\python.exe -m app.scripts.gmail_oauth'"
            )
        from google_auth_oauthlib.flow import InstalledAppFlow

        credentials_path = _configured_path(
            settings.gmail_credentials_file, "GMAIL_CREDENTIALS_FILE"
        )
        if not credentials_path.is_file():
            raise RuntimeError("GMAIL_CREDENTIALS_FILE does not exist")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), GMAIL_SCOPES
        )
        credentials = flow.run_local_server(port=0)
        changed = True

    if changed:
        _save_credentials(token_path, credentials.to_json())
    return credentials


def _configured_path(value: str, variable_name: str) -> Path:
    if not value:
        raise RuntimeError(f"{variable_name} is not configured")
    return Path(value).expanduser()


def _save_credentials(token_path: Path, serialized_credentials: str) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = token_path.with_name(f".{token_path.name}.tmp")
    try:
        temporary_path.write_text(serialized_credentials, encoding="utf-8")
        os.replace(temporary_path, token_path)
    finally:
        temporary_path.unlink(missing_ok=True)
