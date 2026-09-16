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


def prepare_secret_files():
    for kind, attribute in (("TOKEN", "gmail_token_file"), ("CREDENTIALS", "gmail_credentials_file")):
        encoded = os.getenv("GMAIL_" + kind + "_JSON_BASE64")
        if not encoded:
            continue
        filename = getattr(settings, attribute)
        if not filename:
            raise ValueError("Gmail secret file path required")
        data = json.loads(base64.b64decode(encoded, validate=True).decode("utf-8-sig"))
        if kind == "TOKEN":
            validate_token_scopes(data)
        elif not isinstance(data, dict) or not (isinstance(data.get("installed"), dict) or isinstance(data.get("web"), dict)):
            raise ValueError("Invalid OAuth client document")
        path = Path(filename)
        if path.is_symlink():
            raise ValueError("Gmail secret file cannot be a symlink")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not path.exists():
            _save_credentials(path, json.dumps(data))
