"""Offline smoke of supplier OAuth secret materialization in Buying backend.

No real Gmail connection, no token exchange, and no message sent.
"""
import ast
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.integrations.email.supplier_sender import GmailSupplierSender


class GmailProfile:
    def execute(self):
        return {"emailAddress": "beauty.lowcoster@gmail.com"}


class FakeGmail:
    def users(self):
        return self

    def getProfile(self, **kwargs):
        assert kwargs["userId"] == "me"
        return GmailProfile()


def main():
    ast.parse(Path("app/integrations/email/supplier_sender.py").read_text(
        encoding="utf-8-sig"
    ))
    with TemporaryDirectory() as folder:
        token_path = Path(folder) / "supplier_token.json"
        credentials_path = Path(folder) / "supplier_credentials.json"
        client = {"installed": {
            "client_id": "dummy-client-id",
            "client_secret": "dummy-client-secret",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }}
        token = {
            "token": "fake-token",
            "refresh_token": "fake-refresh-token",
            "client_id": "dummy-client-id",
            "client_secret": "dummy-client-secret",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": [
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/gmail.send",
            ],
        }
        keys = {
            "SUPPLIER_GMAIL_TOKEN_FILE": str(token_path),
            "SUPPLIER_GMAIL_CREDENTIALS_FILE": str(credentials_path),
            "SUPPLIER_GMAIL_TOKEN_JSON": json.dumps(token),
            "SUPPLIER_GMAIL_CREDENTIALS_JSON": json.dumps(client),
            "SUPPLIER_GMAIL_USER_ID": "me",
            "SUPPLIER_GMAIL_EXPECTED_EMAIL": "beauty.lowcoster@gmail.com",
            "CUSTOMER_GMAIL_EXPECTED_EMAIL": "wowhairlover@gmail.com",
        }
        old_env = {k: os.environ.get(k) for k in keys}
        try:
            os.environ.update(keys)

            def fake_load(*, allow_interactive, mailbox_role):
                assert not allow_interactive and mailbox_role == "supplier"
                assert json.loads(token_path.read_text()) == token
                assert json.loads(credentials_path.read_text()) == client
                return object()

            with patch("app.integrations.email.gmail_auth.load_gmail_credentials",
                       side_effect=fake_load), patch(
                       "googleapiclient.discovery.build",
                       return_value=FakeGmail()), patch(
                       "google_auth_httplib2.AuthorizedHttp",
                       return_value=object()), patch(
                       "httplib2.Http", return_value=object()), patch(
                       "app.integrations.http_tls.verified_ca_bundle",
                       return_value="/dev/null"):
                result = GmailSupplierSender()._service()
            assert isinstance(result, FakeGmail)
            assert not any(k.startswith("CUSTOMER_GMAIL_") and
                           k not in {"CUSTOMER_GMAIL_EXPECTED_EMAIL"}
                           for k in keys), "No customer secrets may be touched"
            # Reading an existing token must not be overwritten by the
            # stale env JSON on a later backend request.
            token_path.write_text('{"refreshed":true}', encoding="utf-8")
            from app.integrations.email.secret_files import prepare_secret_files
            prepare_secret_files("supplier", overwrite_json=False)
            assert token_path.read_text() == '{"refreshed":true}'
        finally:
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    print("BUYING_SUPPLIER_GMAIL_SECRET_LOADER_SMOKE_OK", flush=True)


if __name__ == "__main__":
    main()
