import os
import json
from pathlib import Path

from app.config.settings import settings


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SCOPES = (GMAIL_READONLY_SCOPE,)


def validate_token_scopes(data, scopes=GMAIL_SCOPES):
    if not isinstance(data, dict):
        raise ValueError("Invalid Gmail token document")
    granted = data.get("scopes", [])
    if isinstance(granted, str):
        granted = granted.split()
    if not isinstance(granted, list) or set(granted) != set(scopes):
        raise ValueError("Gmail token must grant exactly gmail.readonly" if tuple(scopes) == GMAIL_SCOPES else "Supplier Gmail token must grant exactly gmail.readonly and gmail.send")


def load_gmail_credentials(*, allow_interactive: bool = False, mailbox_role='customer'):
    """Load/refresh Gmail credentials, optionally running desktop OAuth once."""
    from google.auth.transport.requests import Request
    from app.integrations.http_tls import verified_session
    from google.oauth2.credentials import Credentials

    from app.integrations.email.mailboxes import mailbox
    config = mailbox(mailbox_role)
    token_path = _configured_path(config.token_file, config.prefix + '_TOKEN_FILE')

    credentials = None
    if token_path.exists():
        validate_token_scopes(json.loads(token_path.read_text(encoding="utf-8-sig")), config.scopes)
        credentials = Credentials.from_authorized_user_file(
            str(token_path), config.scopes
        )

    changed = False
    if credentials and credentials.expired and credentials.refresh_token:
        class BoundedRequest(Request):
            def __call__(self, *args, **kwargs):
                kwargs["timeout"] = (5, 20)
                return super().__call__(*args, **kwargs)
        from google.auth.exceptions import RefreshError
        try:
            with verified_session() as session:
                credentials.refresh(BoundedRequest(session=session))
            changed = True
        except RefreshError as error:
            from app.integrations.email.health import classify
            if not allow_interactive or classify(error) != 'reauth_required':
                raise
            # Preserve the old file until explicit desktop OAuth succeeds.
            credentials = None

    if not credentials or not credentials.valid:
        if not allow_interactive:
            raise RuntimeError(
                "Gmail authorization is required; run "
                "'.\\.venv\\Scripts\\python.exe -m app.scripts.gmail_oauth'"
            )
        from google_auth_oauthlib.flow import InstalledAppFlow

        credentials_path = _configured_path(
            config.credentials_file, config.prefix + '_CREDENTIALS_FILE'
        )
        if not credentials_path.is_file():
            raise RuntimeError("GMAIL_CREDENTIALS_FILE does not exist")
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), config.scopes
        )
        credentials = flow.run_local_server(port=0)
        changed = True

    if changed:
        _save_credentials(token_path, credentials.to_json())
    return credentials


def _configured_path(value: str, variable_name: str) -> Path:
    if not value:
        from app.integrations.email.health import MailboxNotConfigured
        raise MailboxNotConfigured(f"{variable_name} is not configured")
    return Path(value).expanduser()


def _save_credentials(token_path: Path, serialized_credentials: str) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = token_path.with_name(f".{token_path.name}.tmp")
    try:
        descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(serialized_credentials)
        os.replace(temporary_path, token_path)
    finally:
        temporary_path.unlink(missing_ok=True)
