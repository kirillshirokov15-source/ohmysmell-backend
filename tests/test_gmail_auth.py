from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from app.config.settings import settings
from app.integrations.email import gmail_auth
from app.integrations.email.gmail_auth import (
    GMAIL_READONLY_SCOPE,
    GMAIL_SCOPES,
)
from app.integrations.email.gmail_provider import GmailEmailProvider


@pytest.fixture
def gmail_paths(tmp_path):
    old_credentials = settings.gmail_credentials_file
    old_token = settings.gmail_token_file
    credentials = tmp_path / "folder with spaces" / "gmail-credentials.json"
    token = tmp_path / "folder with spaces" / "gmail-token.json"
    credentials.parent.mkdir()
    credentials.write_text("{}", encoding="utf-8")
    settings.gmail_credentials_file = str(credentials)
    settings.gmail_token_file = str(token)
    try:
        yield credentials, token
    finally:
        settings.gmail_credentials_file = old_credentials
        settings.gmail_token_file = old_token


def test_scope_is_exactly_gmail_readonly():
    assert GMAIL_SCOPES == (GMAIL_READONLY_SCOPE,)
    assert GMAIL_READONLY_SCOPE == (
        "https://www.googleapis.com/auth/gmail.readonly"
    )


def test_provider_never_requests_interactive_authorization():
    credentials = Mock()
    with (
        patch(
            "app.integrations.email.gmail_provider.load_gmail_credentials",
            return_value=credentials,
        ) as load,
        patch("googleapiclient.discovery.build", return_value=Mock()),
    ):
        GmailEmailProvider()._build_service()
    load.assert_called_once_with(allow_interactive=False)


def test_missing_token_is_a_controlled_noninteractive_error(gmail_paths):
    with pytest.raises(RuntimeError, match="gmail_oauth"):
        gmail_auth.load_gmail_credentials(allow_interactive=False)


def test_interactive_flow_uses_configured_paths_with_spaces(gmail_paths):
    credentials_path, token_path = gmail_paths
    credentials = SimpleNamespace(valid=True, to_json=lambda: "TOKEN_SECRET")
    flow = Mock()
    flow.run_local_server.return_value = credentials
    with (
        patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
            return_value=flow,
        ) as build_flow,
        patch.object(gmail_auth, "_save_credentials") as save,
    ):
        result = gmail_auth.load_gmail_credentials(allow_interactive=True)

    assert result is credentials
    build_flow.assert_called_once_with(str(credentials_path), GMAIL_SCOPES)
    flow.run_local_server.assert_called_once_with(port=0)
    save.assert_called_once_with(token_path, "TOKEN_SECRET")


def test_expired_token_refreshes_and_is_saved(gmail_paths):
    _, token_path = gmail_paths
    token_path.write_text(__import__("json").dumps({"scopes": list(GMAIL_SCOPES)}), encoding="utf-8")
    credentials = Mock(
        expired=True,
        refresh_token="refresh-secret",
        valid=False,
    )
    credentials.to_json.return_value = "REFRESHED_TOKEN_SECRET"

    def mark_valid(_request):
        credentials.valid = True

    credentials.refresh.side_effect = mark_valid
    with (
        patch(
            "google.oauth2.credentials.Credentials.from_authorized_user_file",
            return_value=credentials,
        ) as load,
        patch.object(gmail_auth, "_save_credentials") as save,
    ):
        gmail_auth.load_gmail_credentials(allow_interactive=False)

    load.assert_called_once_with(str(token_path), GMAIL_SCOPES)
    credentials.refresh.assert_called_once()
    save.assert_called_once_with(token_path, "REFRESHED_TOKEN_SECRET")


def test_auth_helper_does_not_log_secrets(gmail_paths, capsys):
    credentials = SimpleNamespace(valid=True, to_json=lambda: "TOKEN_SECRET")
    flow = Mock()
    flow.run_local_server.return_value = credentials
    with (
        patch(
            "google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file",
            return_value=flow,
        ),
        patch.object(gmail_auth, "_save_credentials"),
    ):
        gmail_auth.load_gmail_credentials(allow_interactive=True)
    output = capsys.readouterr()
    assert "TOKEN_SECRET" not in output.out + output.err
    assert "refresh-secret" not in output.out + output.err


def test_revoked_refresh_stops_without_interactive_oauth(gmail_paths, capsys):
    import json
    from google.auth.exceptions import RefreshError
    _, token_path = gmail_paths
    token_path.write_text(json.dumps({"scopes": list(GMAIL_SCOPES)}), encoding="utf-8")
    credentials = Mock(expired=True, refresh_token="PRIVATE", valid=False)
    credentials.refresh.side_effect = RefreshError("PRIVATE")
    with (
        patch("google.oauth2.credentials.Credentials.from_authorized_user_file", return_value=credentials),
        patch("google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file") as interactive,
        patch.object(gmail_auth, "_save_credentials") as save,
    ):
        with pytest.raises(RefreshError):
            gmail_auth.load_gmail_credentials(allow_interactive=False)
        interactive.assert_not_called()
        save.assert_not_called()
    assert "PRIVATE" not in capsys.readouterr().out


def test_oauth_cli_only_invokes_explicit_interactive_auth(monkeypatch, capsys):
    from app.scripts import gmail_oauth

    authorize = Mock()
    monkeypatch.setattr(gmail_oauth, "load_gmail_credentials", authorize)
    monkeypatch.setattr(settings, "gmail_token_file", "C:/safe/gmail-token.json")
    gmail_oauth.main()
    authorize.assert_called_once_with(allow_interactive=True)
    assert "Gmail OAuth authorization: OK" in capsys.readouterr().out


def test_smoke_test_uses_read_only_profile_and_inbox_list_only():
    service = Mock()
    users = service.users.return_value
    users.getProfile.return_value.execute.return_value = {
        "emailAddress": "account@example.com"
    }
    users.messages.return_value.list.return_value.execute.return_value = {
        "messages": [{"id": str(index)} for index in range(5)]
    }

    result = GmailEmailProvider(service).readonly_smoke_check(max_messages=99)

    assert result == {
        "account": "account@example.com",
        "messages_found": 5,
    }
    users.getProfile.assert_called_once_with(userId=settings.gmail_user_id)
    users.messages.return_value.list.assert_called_once_with(
        userId=settings.gmail_user_id,
        labelIds=["INBOX"],
        maxResults=5,
    )
    users.messages.return_value.get.assert_not_called()


def test_cli_modules_do_not_import_database_or_ingestion():
    oauth_source = Path("app/scripts/gmail_oauth.py").read_text(encoding="utf-8")
    smoke_source = Path("app/scripts/gmail_smoke_test.py").read_text(
        encoding="utf-8"
    )
    combined = oauth_source + smoke_source
    assert "database" not in combined.casefold()
    assert "ingestion" not in combined.casefold()
