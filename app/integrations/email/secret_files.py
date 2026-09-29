"""Materialize Railway secret env values into private files; never log their contents.
Existing token files are retained so refreshed credentials survive on a volume.
Ephemeral containers reconstruct from the configured refresh token on each boot.
"""
import base64
import json
import os
from pathlib import Path
from app.config.settings import settings
from app.integrations.email.gmail_auth import validate_token_scopes, _save_credentials


def prepare_secret_files(mailbox_role='customer', *, overwrite_json=True):
    from app.integrations.email.mailboxes import mailbox
    import tempfile
    config = mailbox(mailbox_role)
    for kind, attribute in (("TOKEN", "gmail_token_file"), ("CREDENTIALS", "gmail_credentials_file")):
        raw = os.getenv(config.prefix + '_' + kind + '_JSON')
        encoded = os.getenv(config.prefix + '_' + kind + '_JSON_BASE64')
        if mailbox_role == 'customer' and not raw and not encoded:
            encoded = os.getenv('GMAIL_' + kind + '_JSON_BASE64')
        if not raw and not encoded:
            continue
        filename = config.token_file if kind == 'TOKEN' else config.credentials_file
        if not filename:
            if not raw and not os.getenv(config.prefix + '_' + kind + '_JSON_BASE64'):
                raise ValueError('Gmail secret file path required')
            filename = str(Path(tempfile.gettempdir()) / 'ohmysmell' / mailbox_role / (kind.lower() + '.json'))
            if mailbox_role == 'customer':
                setattr(settings, attribute, filename)
            else:
                os.environ[config.prefix + '_' + kind + '_FILE'] = filename
        mailbox(mailbox_role)  # Recheck isolation after resolving an automatic JSON path.
        data = json.loads(raw if raw else base64.b64decode(encoded, validate=True).decode("utf-8-sig"))
        if kind == "TOKEN":
            validate_token_scopes(data, config.scopes)
        elif not isinstance(data, dict) or not (isinstance(data.get("installed"), dict) or isinstance(data.get("web"), dict)):
            raise ValueError("Invalid OAuth client document")
        path = Path(filename)
        if path.is_symlink():
            raise ValueError("Gmail secret file cannot be a symlink")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # Explicit JSON is authoritative on restart; legacy base64 retains old behavior.
        if (raw and overwrite_json) or not path.exists():
            _save_credentials(path, json.dumps(data))
