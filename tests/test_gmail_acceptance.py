import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.config.settings import settings
from app.integrations.email.gmail_auth import GMAIL_SCOPES, validate_token_scopes
from app.integrations.email.gmail_provider import GmailEmailProvider, ReadOnlyHttp


@pytest.mark.parametrize("mode", ["bootstrap", "history", "expired"])
def test_selector_cannot_ingest_other_mail(mode, monkeypatch):
    service = Mock()
    service.users.return_value.getProfile.return_value.execute.return_value = {"historyId": "12"}
    provider = GmailEmailProvider(service, allowed_message_ids=["abc123"])
    history = Mock(return_value=(["bad456", "abc123"], "12"))
    if mode == "expired":
        error = RuntimeError("expired")
        error.resp = SimpleNamespace(status=404)
        history.side_effect = error
    monkeypatch.setattr(provider, "_message_ids_from_history", history)
    get = Mock(return_value="selected")
    monkeypatch.setattr(provider, "_get_message", get)
    result = provider._fetch_sync(None if mode == "bootstrap" else "11")
    assert result.messages == ["selected"] and result.next_cursor == "12"
    get.assert_called_once_with(service, "abc123")
    service.users.return_value.messages.return_value.list.assert_not_called()


def test_cursor_account_and_selector_are_isolated():
    service = Mock()
    profile = service.users.return_value.getProfile.return_value.execute
    profile.return_value = {"emailAddress": "first@example.com"}
    first = GmailEmailProvider(service, allowed_message_ids=["ab"]).cursor_key()
    second = GmailEmailProvider(service, allowed_message_ids=["cd"]).cursor_key()
    profile.return_value = {"emailAddress": "second@example.com"}
    third = GmailEmailProvider(service, allowed_message_ids=["ab"]).cursor_key()
    assert len({first, second, third}) == 3
    assert len(first) <= 50 and "@" not in first


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_transport_blocks_all_gmail_writes(method):
    http = Mock()
    with pytest.raises(RuntimeError, match="GET only"):
        ReadOnlyHttp(http).request("https://gmail.googleapis.com/", method)
    http.request.assert_not_called()


def test_transport_delegates_read_and_close():
    http = Mock()
    transport = ReadOnlyHttp(http)
    transport.request("url", "GET", headers={})
    transport.close()
    http.request.assert_called_once_with("url", "GET", headers={})
    http.close.assert_called_once()


@pytest.mark.parametrize("scopes", [[], [*GMAIL_SCOPES, "gmail.modify"], ["gmail.send"]])
def test_nonreadonly_tokens_rejected(scopes):
    with pytest.raises(ValueError, match="exactly gmail.readonly"):
        validate_token_scopes({"scopes": scopes})


def test_secret_reconstruction_preserves_refreshed_file(tmp_path, monkeypatch, capsys):
    from app.integrations.email.secret_files import prepare_secret_files
    path = tmp_path / "private" / "token.json"
    monkeypatch.setattr(settings, "gmail_token_file", str(path))
    monkeypatch.delenv("GMAIL_CREDENTIALS_JSON_BASE64", raising=False)
    document = {"scopes": list(GMAIL_SCOPES), "refresh_token": "PRIVATE"}
    monkeypatch.setenv("GMAIL_TOKEN_JSON_BASE64", base64.b64encode(json.dumps(document).encode()).decode())
    prepare_secret_files()
    assert json.loads(path.read_text()) == document
    path.write_text("REFRESHED")
    prepare_secret_files()
    assert path.read_text() == "REFRESHED"
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize("value", ["not-base64!", "bnVsbA=="])
def test_malformed_secret_fails_without_file(tmp_path, monkeypatch, value):
    from app.integrations.email.secret_files import prepare_secret_files
    path = tmp_path / "token.json"
    monkeypatch.setattr(settings, "gmail_token_file", str(path))
    monkeypatch.setenv("GMAIL_TOKEN_JSON_BASE64", value)
    with pytest.raises(ValueError):
        prepare_secret_files()
    assert not path.exists()


@pytest.mark.parametrize("status", [401, 429, 500])
def test_gmail_failure_preserves_cursor_and_retries_next_poll(status):
    from app.workers.email_ingestion import EmailIngestionWorker
    from app.integrations.email.provider import EmailFetchBatch
    error = RuntimeError("PRIVATE")
    error.resp = SimpleNamespace(status=status)
    provider = SimpleNamespace(fetch_unprocessed=AsyncMock(side_effect=[error, EmailFetchBatch([], "13")]))
    cursor = SimpleNamespace(get=AsyncMock(return_value="12"), set=AsyncMock())
    pipeline = SimpleNamespace(ingest_email=AsyncMock())
    async def scenario():
        worker = EmailIngestionWorker(provider, pipeline, cursor)
        with pytest.raises(RuntimeError):
            await worker.run_once()
        cursor.set.assert_not_called()
        pipeline.ingest_email.assert_not_called()
        assert await worker.run_once() == {"received": 0, "processed": 0, "failed": 0}
        cursor.set.assert_awaited_once_with("gmail", "13")
    asyncio.run(scenario())


def test_staging_worker_requires_explicit_selector(tmp_path, monkeypatch):
    from app.workers.email_runtime import validate_email
    token = tmp_path / "token.json"
    token.write_text("{}")
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(settings, "gmail_token_file", str(token))
    monkeypatch.setattr(settings, "external_writes_enabled", False)
    monkeypatch.delenv("GMAIL_ALLOWED_MESSAGE_IDS", raising=False)
    with pytest.raises(ValueError, match="allowed message IDs"):
        validate_email()
