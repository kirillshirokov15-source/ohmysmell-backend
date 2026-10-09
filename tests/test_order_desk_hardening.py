import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services.desk_status import public_summary, stage_label
from app.services.desk_delivery_policy import DeskDeliveryPolicy, DeskDeliveryConfigurationError
from app.config.settings import settings
from app.integrations.tilda import normalize, parse_body, TildaInvalid, TildaConfigurationError
from tests.test_tilda_orders import tilda_config, payload


@pytest.mark.parametrize('old_stage', ['new', 'working', 'awaiting_confirmation', 'awaiting_payment'])
@pytest.mark.parametrize('payment,fulfillment,delivery,label', [
    ('unpaid','new','pending','Ожидает оплаты'), ('paid','new','pending','Оплачен'),
    ('paid','assembling','pending','В сборке'), ('paid','assembled','pending','Собран'),
    ('paid','shipped','dispatched','Отгружен'), ('paid','shipped','delivered','Доставлен'),
    ('unpaid','cancelled','cancelled','Отменён')])
def test_order_is_authoritative_for_every_old_desk_stage(old_stage,payment,fulfillment,delivery,label):
    desk=SimpleNamespace(draft_id=102,order_id=205,stage=old_stage)
    draft=SimpleNamespace(status='new')
    order=SimpleNamespace(id=205,status='new',payment_status=payment,fulfillment_status=fulfillment,delivery_status=delivery)
    assert stage_label(desk,draft,order)==label
    summary=public_summary(desk,draft,order)
    assert label in summary
    if payment=='paid': assert 'Ожидает оплаты' not in summary


@pytest.mark.parametrize('stage,label', [('new','Получен'),('working','В работе'),
    ('awaiting_confirmation','Ожидает подтверждения'),('awaiting_payment','Ожидает подтверждения')])
def test_unconfirmed_draft_never_claims_payment_is_due(stage,label):
    desk=SimpleNamespace(draft_id=1,order_id=None,stage=stage)
    draft=SimpleNamespace(status='needs_review')
    assert stage_label(desk,draft)==label
    draft.status='rejected'
    assert stage_label(desk,draft)=='Отменена'


@pytest.mark.parametrize('boundary', ['', 'not-a-date', '2026-10-09T10:00:00'])
def test_draft_scope_requires_explicit_timezone_boundary(monkeypatch,boundary):
    monkeypatch.setattr(settings,'environment','staging')
    monkeypatch.setenv('ORDER_DESK_STAGING_DRAFT_IDS','1')
    monkeypatch.setenv('ORDER_DESK_STAGING_NOT_BEFORE',boundary)
    with pytest.raises(DeskDeliveryConfigurationError): DeskDeliveryPolicy.load('client')


def test_old_draft_message_needs_individual_approval(monkeypatch):
    monkeypatch.setattr(settings,'environment','staging')
    for key,value in {'CLIENT_RECIPIENT_IDS':'123','MESSAGE_IDS':'5','DRAFT_IDS':'9','NOT_BEFORE':'2026-10-09T10:00:00Z'}.items():
        monkeypatch.setenv('ORDER_DESK_STAGING_'+key,value)
    p=DeskDeliveryPolicy.load('client')
    old=SimpleNamespace(id=4,draft_id=9,destination=123,created_at=p.not_before-timedelta(seconds=1))
    assert not p.permits(old)
    old.id=5
    assert p.permits(old)
    old.destination=456
    assert not p.permits(old)


def test_manager_allowlist_rejects_private_recipients(monkeypatch):
    monkeypatch.setattr(settings,'environment','staging')
    monkeypatch.setenv('ORDER_DESK_STAGING_MANAGER_CHAT_IDS','123')
    monkeypatch.delenv('ORDER_DESK_STAGING_DRAFT_IDS',raising=False)
    with pytest.raises(DeskDeliveryConfigurationError):DeskDeliveryPolicy.load('manager')


def test_disabled_sender_does_not_touch_database(monkeypatch):
    import app.workers.desk_outbox as module
    monkeypatch.setenv('ORDER_DESK_SEND_ENABLED','false')
    def forbidden(): raise AssertionError('DB must not be touched')
    monkeypatch.setattr(module,'async_session',forbidden)
    bot=SimpleNamespace(send_message=AsyncMock())
    assert asyncio.run(module.run_once(bot,'client')) is False
    bot.send_message.assert_not_awaited()


def test_http_health_and_readiness_over_loopback():
    from aiohttp import ClientSession, web
    from app.bot.runtime import worker_health_app
    async def run():
        failure=asyncio.Event();state={'polling':False,'role':'client','external_writes':False}
        runner=web.AppRunner(worker_health_app(state,failure));await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        port=runner.addresses[0][1]
        try:
            async with ClientSession() as client:
                async def status(path):
                    async with client.get(f'http://127.0.0.1:{port}{path}') as response:
                        return response.status, await response.json()
                assert (await status('/health'))[0]==200
                assert (await status('/ready'))[0]==503
                state['polling']=True
                assert (await status('/ready'))==(200,state)
                failure.set()
                assert (await status('/health'))[0]==503
                assert (await status('/ready'))[0]==503
        finally: await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('value',['bad-private-value','0','65536','-1'])
def test_invalid_worker_port_has_safe_category(monkeypatch,value):
    from app.bot.runtime import worker_port
    from app.bot.worker_errors import WorkerConfigurationError, startup_error_category
    monkeypatch.setenv('PORT',value)
    with pytest.raises(WorkerConfigurationError) as caught: worker_port()
    assert startup_error_category(caught.value)=='invalid_worker_port'
    assert value not in str(caught.value)


@pytest.mark.parametrize('content_type,body', [
    ('application/json',b'{"tranid":"a","tranid":"b"}'),
    ('application/json',b'{"value":NaN}'),
    ('application/x-www-form-urlencoded',b'products[0][name]=a&products[0]=b'),
    ('application/x-www-form-urlencoded',b'products[00][name]=a'),
    ('application/x-www-form-urlencoded',b'payment=x&payment[products][0][name]=a'),
    ('application/x-www-form-urlencoded',b'products[1][name]=a'),
    ('application/x-www-form-urlencoded',b'products[0][name]=a&products[name]=b')])
def test_ambiguous_payloads_fail_closed(content_type,body):
    with pytest.raises(TildaInvalid):parse_body(body,content_type)


@pytest.mark.parametrize('variable,value',[('TILDA_FIELD_MAP_JSON','[]'),('TILDA_ITEM_FIELD_MAP_JSON','{"id":12}'),
    ('TILDA_FIELD_MAP_JSON','{"items":""}'),('TILDA_FIELD_MAP_JSON','{"unrecognized":"products"}'),
    ('TILDA_ITEM_FIELD_MAP_JSON','private-malformed-value')])
def test_bad_mapping_configuration_is_safe(tilda_config,monkeypatch,variable,value):
    monkeypatch.setenv(variable,value)
    with pytest.raises(TildaConfigurationError) as caught:normalize(payload())
    assert str(caught.value)=='invalid_tilda_field_map'


def test_synthetic_transport_contract_fixtures(tilda_config,monkeypatch):
    cases=json.loads(Path('tests/fixtures/tilda/contracts.json').read_text(encoding='utf-8'))
    normalized=[]
    for case in cases:
        monkeypatch.setenv('TILDA_FIELD_MAP_JSON',json.dumps(case['fields']))
        monkeypatch.setenv('TILDA_ITEM_FIELD_MAP_JSON',json.dumps(case['item_fields']))
        normalized.append(normalize(parse_body(case['body'].encode(),case['content_type'])))
    assert all(row==normalized[0] for row in normalized)
    assert normalized[0]['reported_total']==3030 and normalized[0]['problems']==[]


def test_identifiers_and_quantity_are_not_coerced_from_unsafe_types(tilda_config):
    for key,value in [('tranid',True),('tranid','   ')]:
        data=payload();data[key]=value
        with pytest.raises(TildaInvalid):normalize(data)
    for key,value in [('externalid',{'id':'sku-a'}),('name',{}),('quantity',2.0)]:
        data=payload();data['products'][0][key]=value
        with pytest.raises(TildaInvalid):normalize(data)
