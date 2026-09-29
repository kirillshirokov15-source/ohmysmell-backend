import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch
import pytest
from google.auth.exceptions import RefreshError
from app.config.settings import settings
from app.integrations.email.mailboxes import READONLY, SEND, mailbox, verify_identity
from app.integrations.email.gmail_auth import load_gmail_credentials, validate_token_scopes
from app.integrations.email.secret_files import prepare_secret_files
from app.integrations.email.gmail_provider import GmailEmailProvider
from app.integrations.email.supplier_gmail import SupplierGmailProvider
from app.workers.email_ingestion import EmailIngestionWorker
from app.workers.supplier_replies import SupplierReplyPoller
from app.services.buying_adapters import DisabledGmailSupplierSender
from tests.test_buying import workspace, login


@pytest.fixture
def mailboxes(tmp_path, monkeypatch):
    for role in ('CUSTOMER', 'SUPPLIER'):
        for suffix in ('TOKEN_JSON','TOKEN_JSON_BASE64','CREDENTIALS_JSON','CREDENTIALS_JSON_BASE64','TOKEN_FILE','CREDENTIALS_FILE'):
            monkeypatch.delenv(role+'_GMAIL_'+suffix, raising=False)
        monkeypatch.setenv(role+'_GMAIL_EXPECTED_EMAIL',role.lower()+'@example.invalid')
    monkeypatch.delenv('GMAIL_TOKEN_JSON_BASE64',raising=False)
    monkeypatch.delenv('GMAIL_CREDENTIALS_JSON_BASE64',raising=False)
    customer=tmp_path/'customer.json'; supplier=tmp_path/'supplier.json'
    monkeypatch.setattr(settings,'gmail_token_file',str(customer))
    monkeypatch.setattr(settings,'gmail_credentials_file',str(tmp_path/'customer-client.json'))
    monkeypatch.setattr(settings,'gmail_user_id','customer@example.invalid')
    monkeypatch.setenv('SUPPLIER_GMAIL_TOKEN_FILE',str(supplier))
    monkeypatch.setenv('SUPPLIER_GMAIL_CREDENTIALS_FILE',str(tmp_path/'supplier-client.json'))
    monkeypatch.setenv('SUPPLIER_GMAIL_USER_ID','supplier@example.invalid')
    return customer,supplier


def test_separate_json_files_users_and_scopes(mailboxes,monkeypatch):
    customer,supplier=mailboxes
    for role,scopes in (('CUSTOMER',[READONLY]),('SUPPLIER',[READONLY,SEND])):
        monkeypatch.setenv(role+'_GMAIL_TOKEN_JSON',json.dumps({'scopes':scopes,'refresh_token':'synthetic-'+role}))
    prepare_secret_files('customer')
    assert customer.exists() and not supplier.exists()
    prepare_secret_files('supplier')
    assert json.loads(customer.read_text())['refresh_token'] != json.loads(supplier.read_text())['refresh_token']
    assert mailbox('customer').user_id != mailbox('supplier').user_id
    with patch('google.oauth2.credentials.Credentials.from_authorized_user_file',return_value=SimpleNamespace(expired=False,valid=True)) as load:
        load_gmail_credentials()
        assert load.call_args.args==(str(customer),(READONLY,))
        load_gmail_credentials(mailbox_role='supplier')
        assert load.call_args.args==(str(supplier),(READONLY,SEND))


@pytest.mark.parametrize('role,scopes',[('customer',[READONLY,SEND]),('supplier',[READONLY]),('supplier',[READONLY,SEND,'https://www.googleapis.com/auth/gmail.modify'])])
def test_cross_scope_tokens_rejected(mailboxes,role,scopes):
    path=mailboxes[role=='supplier']
    path.write_text(json.dumps({'scopes':scopes}))
    with patch('google.oauth2.credentials.Credentials.from_authorized_user_file') as load:
        with pytest.raises(ValueError):load_gmail_credentials(mailbox_role=role)
        load.assert_not_called()


def test_shared_token_path_forbidden(mailboxes,monkeypatch):
    monkeypatch.setenv('SUPPLIER_GMAIL_TOKEN_FILE',str(mailboxes[0]))
    with pytest.raises(ValueError):mailbox('supplier')


def test_json_only_configuration_uses_distinct_private_paths(mailboxes,monkeypatch,tmp_path):
    monkeypatch.setattr('tempfile.gettempdir',lambda:str(tmp_path))
    monkeypatch.setattr(settings,'gmail_token_file','')
    monkeypatch.setattr(settings,'gmail_credentials_file','')
    monkeypatch.delenv('SUPPLIER_GMAIL_TOKEN_FILE')
    monkeypatch.delenv('SUPPLIER_GMAIL_CREDENTIALS_FILE')
    monkeypatch.setenv('CUSTOMER_GMAIL_TOKEN_JSON',json.dumps({'scopes':[READONLY]}))
    monkeypatch.setenv('SUPPLIER_GMAIL_TOKEN_JSON',json.dumps({'scopes':[READONLY,SEND]}))
    prepare_secret_files('customer'); prepare_secret_files('supplier')
    assert mailbox('customer').token_file != mailbox('supplier').token_file
    from pathlib import Path
    assert Path(mailbox('customer').token_file).is_file()
    assert Path(mailbox('supplier').token_file).is_file()


def test_generated_path_cannot_overwrite_other_mailbox(mailboxes,monkeypatch,tmp_path):
    monkeypatch.setattr('tempfile.gettempdir',lambda:str(tmp_path))
    monkeypatch.setattr(settings,'gmail_token_file','')
    path=tmp_path/'ohmysmell'/'customer'/'token.json'
    path.parent.mkdir(parents=True)
    path.write_text('PRIVATE_SENTINEL')
    monkeypatch.setenv('SUPPLIER_GMAIL_TOKEN_FILE',str(path))
    monkeypatch.setenv('CUSTOMER_GMAIL_TOKEN_JSON',json.dumps({'scopes':[READONLY]}))
    with pytest.raises(ValueError):prepare_secret_files('customer')
    assert path.read_text()=='PRIVATE_SENTINEL'


def test_identity_cannot_be_reused(mailboxes,monkeypatch):
    assert verify_identity('customer',{'emailAddress':'customer@example.invalid'})=='customer@example.invalid'
    assert verify_identity('supplier',{'emailAddress':'supplier@example.invalid'})=='supplier@example.invalid'
    for role,account in [('customer','supplier@example.invalid'),('supplier','customer@example.invalid')]:
        with pytest.raises(ValueError):verify_identity(role,{'emailAddress':account})
    monkeypatch.setenv('SUPPLIER_GMAIL_EXPECTED_EMAIL','customer@example.invalid')
    with pytest.raises(ValueError):verify_identity('supplier',{'emailAddress':'customer@example.invalid'})


def test_customer_cannot_send_or_poll_supplier_and_supplier_cannot_ingest(mailboxes):
    customer=GmailEmailProvider(service=Mock())
    supplier=SupplierGmailProvider(service=Mock())
    with pytest.raises(ValueError):DisabledGmailSupplierSender(customer)
    with pytest.raises(ValueError):SupplierReplyPoller(customer)
    with pytest.raises(ValueError):EmailIngestionWorker(provider=supplier)
    assert not hasattr(supplier,'fetch_unprocessed')
    with pytest.raises(ValueError,match='disabled'):
        asyncio.run(DisabledGmailSupplierSender(supplier).send(key='test',recipient='test@example.invalid',body='Item – 2 шт.'))
    supplier.service.users.assert_not_called()


def test_supplier_smoke_never_sends(mailboxes):
    service=Mock()
    service.users.return_value.getProfile.return_value.execute.return_value={'emailAddress':'supplier@example.invalid'}
    service.users.return_value.messages.return_value.list.return_value.execute.return_value={'messages':[]}
    result=SupplierGmailProvider(service=service).readonly_smoke_check()
    assert result['send_scope_present'] and result['email_sent'] is False
    service.users.return_value.messages.return_value.send.assert_not_called()
    service.users.return_value.messages.return_value.modify.assert_not_called()


def test_independent_health_rows_and_restricted_status(workspace,monkeypatch):
    from app.services import integration_health as health
    c,factory,_=workspace
    monkeypatch.setattr(health,'async_session',factory)
    async def record():
        await health.record_mailbox('customer','reauth_required',900)
        await health.record_mailbox('supplier','connected',60)
    asyncio.run(record())
    login(c)
    response=c.get('/buying/settings/status').json()
    assert response['customer_gmail']['status']=='reauth_required'
    assert response['supplier_gmail']['status']=='connected'
    assert 'gmail' not in response and 'token' not in str(response)
    login(c,'picker')
    assert c.get('/buying/settings/status').status_code==403


@pytest.mark.parametrize('failed_role',['customer','supplier'])
def test_failure_in_one_runtime_does_not_stop_other(mailboxes,monkeypatch,failed_role):
    from app.workers import email_runtime as runtime
    import app.database.session as database
    import app.services.integration_health as health
    import app.integrations.email.secret_files as files
    import app.integrations.email.mailboxes as config
    from aiohttp import web
    monkeypatch.setattr(runtime,'validate_email',lambda:None)
    monkeypatch.setattr(files,'prepare_secret_files',lambda *a:None)
    monkeypatch.setattr(config,'configured',lambda *a:True)
    stops=[]; states={}
    monkeypatch.setattr(runtime,'install_stop_signals',lambda loop,stop:stops.append(stop) or (lambda:None))
    context=AsyncMock(); connection=AsyncMock(); context.__aenter__.return_value=connection; connection.scalar.return_value=True
    monkeypatch.setattr(database,'engine',SimpleNamespace(connect=Mock(return_value=context),dispose=AsyncMock()))
    monkeypatch.setattr(web,'AppRunner',lambda app:SimpleNamespace(setup=AsyncMock(),cleanup=AsyncMock()))
    monkeypatch.setattr(web,'TCPSite',lambda *a:SimpleNamespace(start=AsyncMock()))
    async def record(role,status,delay):
        states[role]=status
        stops[0 if role=='customer' else 1].set()
    async def customer_record(status,delay):await record('customer',status,delay)
    monkeypatch.setattr(health,'record_email',customer_record)
    monkeypatch.setattr(health,'record_mailbox',record)
    workers={r:SimpleNamespace(run_once=AsyncMock(side_effect=RefreshError('invalid_grant PRIVATE') if r==failed_role else None,return_value={'received':0,'processed':0,'failed':0})) for r in ('customer','supplier')}
    async def run():
        await asyncio.gather(runtime.run(workers['customer']),runtime.run(workers['supplier'],mailbox_role='supplier'))
    asyncio.run(run())
    assert states[failed_role]=='reauth_required'
    assert states['supplier' if failed_role=='customer' else 'customer']=='connected'
    assert all(w.run_once.await_count==1 for w in workers.values())
