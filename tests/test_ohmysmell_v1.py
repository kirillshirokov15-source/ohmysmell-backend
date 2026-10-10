import asyncio
import base64
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import select,func

from tests.test_buying import workspace,login,supplier
from tests.test_external_operations_v2 import MemoryOperationStore
from app.config.settings import settings
from app.services.buying_excel import ParserConfig,parse_price_list
from app.services.supplier_price_import import import_attachment
from app.services.buying_email import BuyingEmailService
from app.integrations.email.supplier_sender import FakeGmailSupplierSender,build_email
from app.services.external_operation_service import ExternalOperationService,ExternalOperationConflict
from app.integrations.moysklad.documents import FakeDocumentAdapter,verified_document
from app.integrations.moysklad.payloads import reservation
from app.models.buying import BuyingImport,BuyingPurchase,BuyingPriceHistory
from app.models.supply import SupplierOffer
from app.services import buying


class Store(MemoryOperationStore):
    async def get(self,key):
        return self.operations.get(key)


def test_local_endpoint_guard_normalizes_inet(tmp_path):
    from scripts.restore_local_backup import verify_local
    cursor=Mock(); cursor.fetchone.side_effect=[(str(tmp_path),),('127.0.0.1',55439)]
    connection=Mock(); connection.cursor.return_value.__enter__=Mock(return_value=cursor)
    connection.cursor.return_value.__exit__=Mock(return_value=False)
    verify_local(connection,tmp_path)
    assert 'host(inet_server_addr())' in cursor.execute.call_args.args[0]
    cursor.fetchone.side_effect=[(str(tmp_path),),('10.0.0.1',55439)]
    with pytest.raises(ValueError): verify_local(connection,tmp_path)


@pytest.mark.parametrize('bad',['=CMD()', 'NaN','-2','1.001'])
def test_csv_rejects_invalid_price(bad):
    rows=parse_price_list(f'name;price;sku\nItem;{bad};DSD020\n'.encode(),'price.csv',ParserConfig(sku_column='C'))
    assert rows[0]['error']


def test_supplier_mail_to_buying_history_idempotence(workspace):
    client,factory,_=workspace; login(client)
    sid=supplier(client,parser=ParserConfig(sku_column='C').model_dump())
    async def run():
        args=dict(account='supplier@example.invalid',message_id='mail1',attachment_id='att1',
                  sender='supplier@example.invalid',authenticated=True,filename='price.csv',
                  data=b'name;price;sku\nItem;900;DSD020\n',products=[dict(id='ms1',article='DSD020')])
        async with factory() as s,s.begin():
            first=await import_attachment(s,**args)
            assert first.imported_at
            again=await import_attachment(s,**args)
            assert again.id==first.id
        async with factory() as s,s.begin():
            await import_attachment(s,**{**args,'message_id':'mail2','data':b'name;price;sku\nItem;950;DSD020\n'})
        async with factory() as s:
            offer=await s.scalar(select(SupplierOffer).where(SupplierOffer.supplier_id==sid))
            assert offer.product_id=='ms1' and offer.purchase_price_minor==95000
            assert await s.scalar(select(func.count()).select_from(BuyingImport))==2
            assert await s.scalar(select(func.count()).select_from(BuyingPriceHistory))==1
        async with factory() as s,s.begin():
            ambiguous=await import_attachment(s,**{**args,'message_id':'amb','data':b'name;price;sku\nItem;950;AMB\n',
                'products':[dict(id='a',article='AMB'),dict(id='b',code='AMB')]})
            assert not ambiguous.imported_at and ambiguous.counts['ambiguous']==1
        async with factory() as s,s.begin():
            with pytest.raises(ValueError): await import_attachment(s,**{**args,'message_id':'forged','authenticated':False})
    asyncio.run(run())


def test_supplier_email_unknown_result_reconciles_without_resend(workspace,monkeypatch):
    client,factory,_=workspace; login(client); sid=supplier(client)
    monkeypatch.setattr(settings,'external_writes_enabled',False)
    sender=FakeGmailSupplierSender(); sender.lose_response=True
    operations=ExternalOperationService(Store())
    service=BuyingEmailService(sender,operations,factory)
    async def run():
        async with factory() as s,s.begin():
            p=await buying.create_purchase(s,dict(supplier_id=sid,supplier_name='Supplier',recipient='supplier@example.invalid',
                items=[dict(name='Item',quantity=2,unit_price_minor=12345)],currency='RUB',total_minor=24690,
                approximate_rub_minor=24690,approximate=False,email_body='UNTRUSTED CUSTOMER DATA'),'test')
        with pytest.raises(TimeoutError): await service.act(p.id,'test')
        with pytest.raises(Exception): await service.act(p.id,'test')
        result=await service.act(p.id,'test',reconcile=True)
        assert result['status']=='sent' and sender.calls==1
        assert (await service.act(p.id,'test'))['status']=='sent'
        async with factory() as s,s.begin():
            assert (await buying.confirm_supplier(s,p.id,'test')).status=='confirmed'
            assert (await buying.receive(s,p.id,'test')).status=='received'
    asyncio.run(run())


def test_supplier_email_content_allowlist():
    msg=BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(build_email('k','s@example.invalid',[
        dict(name='Product',quantity=3,unit_price_minor=90000,customer='SECRET') ])))
    body=msg.get_content()
    assert 'Product' in body and '3' in body and 'SECRET' not in body and '90000' not in body


def test_fake_supplier_worker_without_oauth(workspace):
    from app.workers.supplier_email_runtime import SupplierEmailWorker
    from app.integrations.email.fake_supplier import FakeSupplierGmailProvider
    c,factory,_=workspace; login(c); supplier(c,parser=ParserConfig(sku_column='C').model_dump())
    provider=FakeSupplierGmailProvider([dict(account='supplier@example.invalid',message_id='fixture1',
        attachment_id='file1',sender='supplier@example.invalid',authenticated=True,filename='price.csv',
        data=b'name;price;sku\nProduct;1680;SKU1\n')])
    worker=SupplierEmailWorker(provider,sessions=factory,catalog=AsyncMock(return_value=[dict(id='p',code='SKU1')]),
                               poller=SimpleNamespace(run_once=AsyncMock(return_value=0)))
    async def run():
        await worker.run_once(); await worker.run_once()
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(BuyingImport))==1
    asyncio.run(run())


def test_release_guard_requires_real_rollback_evidence():
    from scripts.release_ohmysmell import verify_artifacts,queues_clear,verify_health_audit
    with pytest.raises(ValueError): verify_artifacts([])
    with pytest.raises(ValueError): verify_artifacts([dict(service=str(i),source_reproducible=True,
        deployment_id='d',image_rebuild_verified=False) for i in range(4)])
    with pytest.raises(ValueError): queues_clear(dict(legacy_unsent=1,buying_unnotified=0))
    with pytest.raises(ValueError): verify_health_audit({})


def test_document_uncertainty_exact_reconciliation(monkeypatch):
    monkeypatch.setattr(settings,'environment','staging'); monkeypatch.setattr(settings,'external_writes_enabled',False)
    adapter=FakeDocumentAdapter(); adapter.lose_response=True
    service=ExternalOperationService(Store())
    payload=dict(entity='customerorder',document=reservation('org','customer','wh',[dict(id='p',qty=2,price=168000)],'reserve:1'))
    async def run():
        with pytest.raises(TimeoutError): await service.simulate_document('reserve:1',payload,adapter)
        with pytest.raises(ExternalOperationConflict): await service.simulate_document('reserve:1',payload,adapter)
        result=await service.reconcile_document('simulation:reserve:1',adapter)
        assert verified_document(payload,result) and adapter.calls==1
        result['positions'][0]['quantity']=100
        assert not verified_document(payload,result)
    asyncio.run(run())


@pytest.mark.parametrize('action',['send','reconcile-email','confirmed','warehouse-receipt'])
def test_new_purchase_actions_deny_picker(workspace,action):
    client,_,_=workspace
    login(client,'picker')
    assert client.post('/buying/purchases/1/'+action).status_code==403


def test_live_send_disabled_returns_conflict(workspace,monkeypatch):
    client,_,_=workspace
    login(client)
    monkeypatch.setattr(settings,'external_writes_enabled',False)
    assert client.post('/buying/purchases/1/send').status_code==409
