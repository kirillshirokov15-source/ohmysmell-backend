import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch
import pytest
from google.auth.exceptions import RefreshError, TransportError
from sqlalchemy.exc import OperationalError
from app.integrations.email.health import classify, retry_delay
from tests.test_gmail_auth import gmail_paths


@pytest.mark.parametrize('error,expected',[(RefreshError('invalid_grant PRIVATE'),'reauth_required'),(RefreshError('invalid_client PRIVATE'),'bad_credentials'),(TransportError('PRIVATE'),'network_unavailable'),(OperationalError('PRIVATE',{},None),'database_unavailable'),(RuntimeError('PRIVATE'),'worker_error')])
def test_error_categories(error,expected):
    assert classify(error)==expected and 'PRIVATE' not in classify(error)


def test_backoff():
    assert retry_delay('reauth_required',1,60)==900
    assert [retry_delay('network_unavailable',i,60) for i in (1,2,3,9)]==[60,120,240,900]


def test_explicit_oauth_recovers_invalid_grant(gmail_paths):
    from app.integrations.email import gmail_auth as auth
    _,token=gmail_paths
    token.write_text(json.dumps({'scopes':list(auth.GMAIL_SCOPES)}),encoding='utf-8')
    old=Mock(expired=True,refresh_token='PRIVATE',valid=False)
    old.refresh.side_effect=RefreshError('invalid_grant PRIVATE')
    fresh=SimpleNamespace(valid=True,to_json=lambda:'NEW_PRIVATE')
    flow=Mock(); flow.run_local_server.return_value=fresh
    with patch('google.oauth2.credentials.Credentials.from_authorized_user_file',return_value=old), patch('google_auth_oauthlib.flow.InstalledAppFlow.from_client_secrets_file',return_value=flow) as build, patch.object(auth,'_save_credentials') as save:
        with pytest.raises(RefreshError): auth.load_gmail_credentials(allow_interactive=False)
        build.assert_not_called(); save.assert_not_called()
        assert auth.load_gmail_credentials(allow_interactive=True) is fresh
        save.assert_called_once_with(token,'NEW_PRIVATE')
        assert build.call_args.args[1]==auth.GMAIL_SCOPES


@pytest.mark.parametrize('mode,chat,users,valid', [('group','','',False),('auto','-100123','1,2',True),('group','-100123','bad',False),('group','-100123','',False),('group','123','1',False),('private','','',True),('auto','','1',False)])
def test_group_startup(monkeypatch,mode,chat,users,valid):
    from app.bot.manager_group import validate_group_config
    for key,value in dict(MANAGER_TELEGRAM_MODE=mode,MANAGER_TELEGRAM_CHAT_ID=chat,MANAGER_TELEGRAM_USER_IDS=users).items():monkeypatch.setenv(key,value)
    if valid: assert validate_group_config() in ('private','group')
    else:
        with pytest.raises(ValueError):validate_group_config()


def test_invalid_grant_runtime_health_and_quiet_backoff(monkeypatch):
    from app.workers import email_runtime as runtime
    import app.database.session as database
    import app.services.integration_health as health
    from aiohttp import web
    monkeypatch.setattr(runtime,'validate_email',lambda:None)
    stopper=[]; server=[]; saved=[]
    monkeypatch.setattr(runtime,'install_stop_signals',lambda loop,stop:stopper.append(stop) or (lambda:None))
    context=AsyncMock(); connection=AsyncMock(); context.__aenter__.return_value=connection; connection.scalar.return_value=True
    monkeypatch.setattr(database,'engine',SimpleNamespace(connect=Mock(return_value=context),dispose=AsyncMock()))
    monkeypatch.setattr(web,'AppRunner',lambda app:server.append(app) or SimpleNamespace(setup=AsyncMock(),cleanup=AsyncMock()))
    monkeypatch.setattr(web,'TCPSite',lambda *a:SimpleNamespace(start=AsyncMock()))
    async def record(status,delay):
        saved.append((status,delay))
        for route in server[0].router.routes():
            if route.method=='GET' and route.resource.canonical=='/ready':
                response=await route.handler(None)
                assert response.status==503 and json.loads(response.text)['status']=='reauth_required'
        stopper[0].set()
    monkeypatch.setattr(health,'record_email',record)
    worker=SimpleNamespace(run_once=AsyncMock(side_effect=RefreshError('invalid_grant PRIVATE')))
    asyncio.run(runtime.run(worker))
    worker.run_once.assert_awaited_once()
    assert saved==[('reauth_required',900)]
