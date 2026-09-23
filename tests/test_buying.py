import asyncio
from io import BytesIO
from zipfile import ZipFile
from xml.sax.saxutils import escape
from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI
from types import SimpleNamespace
from tests.asgi_client import request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.database.base import Base
from app.api import buying as api
from app.config.settings import settings
from app.services import buying as service
from app.services.buying_excel import ColumnPriceListParser, ParserConfig
from app.models.buying import BuyingPurchase, BuyingEvent, BuyingReply, BuyingPriceHistory, BuyingProduct
from app.models.supply import SupplierOffer


class TestClient:
    __test__ = False
    def __init__(self, app):
        self.app, self.headers = app, {}

    def __getattr__(self, method):
        def call(path, json=None, headers=None, content=None):
            status, data, response_headers = asyncio.run(request(self.app, method.upper(), path, json, {**self.headers, **(headers or {})}, content, raise_errors=True))
            return SimpleNamespace(status_code=status, text=str(data), json=lambda:data, headers={k.decode():v.decode() for k,v in response_headers.items()})
        return call

    def close(self):
        pass


def xlsx(rows, sheet=1):
    stream = BytesIO()
    with ZipFile(stream, 'w') as z:
        body = ''.join(f'<row r="{n}">' + ''.join(f'<c r="{chr(65+i)}{n}" t="inlineStr"><is><t>{escape(str(v))}</t></is></c>' for i,v in enumerate(row)) + '</row>' for n,row in enumerate(rows, 1))
        z.writestr(f'xl/worksheets/sheet{sheet}.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+body+'</sheetData></worksheet>')
    return stream.getvalue()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///' + str(tmp_path / 'buying.db'))
    async def setup():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    asyncio.run(setup())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(api, 'async_session', factory)
    # SQLite verifies persistence/atomicity; PostgreSQL tests exercise real locks.
    monkeypatch.setattr(service, 'transaction_lock', AsyncMock())
    monkeypatch.setattr(settings, 'buying_shared_password', 'synthetic-password-only')
    monkeypatch.setattr(settings, 'buying_session_secret', 'synthetic-session-secret-for-tests-only')
    monkeypatch.setattr(settings, 'environment', 'staging')
    api.attempts.clear()
    app = FastAPI()
    app.include_router(api.auth_router)
    app.include_router(api.router)
    client = TestClient(app)
    yield client, factory, engine
    client.close()
    asyncio.run(engine.dispose())


def login(client):
    response = client.post('/buying/auth/login', json={'password':'synthetic-password-only'})
    assert response.status_code == 200, response.text
    client.headers['Authorization'] = 'Bearer '+response.json()['access_token']
    return response.json()


def supplier(client, currency='RUB', parser=None):
    response = client.post('/buying/suppliers', json=dict(name='SYNTHETIC '+currency,email='supplier@example.invalid',currency=currency,
        parser=parser or ParserConfig().model_dump()))
    assert response.status_code == 200, response.text
    return response.json()['id']


def preview(client, sid, rows, sheet=1):
    response = client.post(f'/buying/suppliers/{sid}/price-lists/preview?filename=synthetic.xlsx', content=xlsx(rows,sheet), headers={'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, sid, data):
    return client.post(f'/buying/suppliers/{sid}/price-lists/import', json={'import_id':data['import_id']})


def test_auth_revocation_and_rotation(workspace, monkeypatch):
    c,_,_ = workspace
    assert c.get('/buying/catalog').status_code == 401
    assert c.post('/buying/auth/login',json={'password':'wrong'}).status_code == 401
    result = login(c)
    assert result['expires_in'] == 28800
    assert c.get('/buying/catalog').headers['cache-control'] == 'no-store'
    assert c.post('/buying/auth/logout').status_code == 200
    assert c.get('/buying/cart').status_code == 401
    login(c)
    monkeypatch.setattr(settings, 'buying_shared_password','rotated-password-for-test')
    assert c.get('/buying/cart').status_code == 401


def test_auth_fail_closed_and_rate_limit(workspace, monkeypatch):
    c,_,_ = workspace
    for _ in range(30):
        assert c.post('/buying/auth/login',json={'password':'wrong'}).status_code == 401
    assert c.post('/buying/auth/login',json={'password':'wrong'}).status_code == 429
    monkeypatch.setattr(settings,'buying_session_secret','')
    assert c.post('/buying/auth/login',json={'password':'wrong'}).status_code == 503


@pytest.mark.parametrize('qty',[0,-1,True,1.5,'2',100001])
def test_quantity_validation(workspace,qty):
    c,_,_ = workspace
    login(c)
    assert c.post('/buying/cart/items',json={'offer_id':1,'quantity':qty}).status_code == 422


def test_buying_complete_persistent_flow(workspace):
    c,factory,engine = workspace
    login(c)
    a,b = supplier(c),supplier(c,'USD',dict(version='columns-v1',sheet=2,first_row=3,name_column='B',price_column='C',sku_column='A'))
    pa = preview(c,a,[['Name','Price'],['SYNTHETIC Perfume 100 ml','12.50']])
    pb = preview(c,b,[['Title'],['SKU','Name','Price'],['SKU-1','SYNTHETIC Perfume 100 ml','2']],2)
    assert confirm(c,a,pa).status_code == confirm(c,b,pb).status_code == 200
    assert confirm(c,a,pa).status_code == 200
    data = c.get('/buying/catalog?q=perfume').json()
    assert data['total'] == 1
    offers = data['products'][0]['offers']
    assert len(offers) == 2
    assert not any(key in str(data) for key in ('sale_price','customer','margin','settlement'))
    oa,ob = [next(o['id'] for o in offers if o['supplier_id']==sid) for sid in (a,b)]
    async def fx():
        async with factory() as s, s.begin():
            o=await s.get(SupplierOffer,ob)
            o.current_fx_rate_to_rub=Decimal('90'); o.fx_source='fake'; o.fx_rate_date=date(2026,9,23)
    asyncio.run(fx())
    for oid in (oa,ob):
        assert c.post('/buying/cart/items',json={'offer_id':oid,'quantity':2}).status_code==200
    assert c.patch(f'/buying/cart/items/{oa}',json={'quantity':3}).status_code==200
    asyncio.run(engine.dispose())
    assert len(c.get('/buying/cart').json()['items'])==2
    before=c.post('/buying/checkout/preview').json()
    changed=preview(c,a,[['Name','Price'],['SYNTHETIC Perfume 100 ml','15']])
    assert changed['counts']['changed']==1
    assert confirm(c,a,changed).status_code==200
    assert c.get('/buying/cart').json()['items'][0]['price_changed_since_added']
    assert c.post('/buying/checkout/confirm',json={'fingerprint':before['fingerprint']},headers={'Idempotency-Key':'stale-preview'}).status_code==409
    current=c.post('/buying/checkout/preview').json()
    assert current==c.post('/buying/checkout/preview').json()
    assert current['suppliers'][0]['email_body']=='SYNTHETIC Perfume 100 ml – 3 шт.'
    assert current['suppliers'][1]['approximate_rub_minor']==36000
    headers={'Idempotency-Key':'synthetic-checkout'}
    sent=c.post('/buying/checkout/confirm',json={'fingerprint':current['fingerprint']},headers=headers)
    assert sent.status_code==200,sent.text
    assert not sent.json()['real_email_sent']
    ids=sent.json()['purchase_ids']; assert len(ids)==2
    assert c.post('/buying/checkout/confirm',json={'fingerprint':current['fingerprint']},headers=headers).json()==sent.json()
    assert c.get('/buying/cart').json()['items']==[]
    for pid in ids:
        sent=c.post(f'/buying/purchases/{pid}/simulate-send')
        assert sent.status_code==200,sent.text
        assert c.post(f'/buying/purchases/{pid}/simulate-send').json()['message_id']==sent.json()['message_id']
    async def replies():
        async with factory() as s,s.begin():
            p=await s.get(BuyingPurchase,ids[0])
            args=dict(message_id='synthetic-reply',thread_id=p.thread_id,sender='supplier@example.invalid',received_at=service.now(),subject='reply',body='Any reply text')
            assert await service.ingest_reply(s,**{**args,'sender':'wrong@example.invalid'}) is None
            first=await service.ingest_reply(s,**args)
            assert (await service.ingest_reply(s,**args)).message_id==first.message_id
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(BuyingReply))==1
    asyncio.run(replies())
    assert len(c.get(f'/buying/purchases/{ids[0]}').json()['replies'])==1
    received=c.post(f'/buying/purchases/{ids[0]}/received')
    assert received.status_code==200,received.text
    assert c.post(f'/buying/purchases/{ids[0]}/received').json()['received_at']==received.json()['received_at']
    assert received.json()['external_ids']['receipt'].startswith('fake-')
    history=c.get(f'/buying/offers/{oa}/price-history').json()['history']
    assert len(history)==1 and history[0]['old_price_minor']==1250 and history[0]['new_price_minor']==1500
    assert c.get('/buying/purchases?limit=1').json()['total']==2
    async def audit():
        async with factory() as s:
            assert await s.scalar(select(func.count()).select_from(BuyingEvent).where(BuyingEvent.action=='received'))==1
    asyncio.run(audit())


def test_invalid_import_atomic_and_ambiguous(workspace):
    c,factory,_=workspace; login(c); sid=supplier(c)
    p=preview(c,sid,[['Name','Price'],['Valid','10'],['Broken','NaN']])
    assert p['counts']['invalid']==1
    assert confirm(c,sid,p).status_code==409
    assert c.get('/buying/catalog').json()['total']==0
    from app.models.supply import ProductSupply
    async def ambiguous():
        async with factory() as s,s.begin():
            s.add_all([ProductSupply(product_id='a',source_type='external'),ProductSupply(product_id='b',source_type='external')]);await s.flush()
            s.add_all([BuyingProduct(product_id=i,name='Same',normalized_name='same') for i in ('a','b')])
    asyncio.run(ambiguous())
    p=preview(c,sid,[['Name','Price'],['Same','10']])
    assert p['counts']['ambiguous']==1 and len(p['rows'][0]['candidates'])==2
    assert confirm(c,sid,p).status_code==409


def test_parser_duplicate_formula_and_archive_safety():
    parser=ColumnPriceListParser(ParserConfig())
    rows=parser.parse(xlsx([['Name','Price'],['Same','1'],['same','2']]))
    assert rows[1]['error']
    for payload in (b'garbage',b'x'*(2*1024*1024+1)):
        with pytest.raises(ValueError): parser.parse(payload)
    for filename,content in [('xl/vbaProject.bin',b'x'),('../evil',b'x'),('xl/worksheets/sheet1.xml',b'<!DOCTYPE a><x/>')]:
        buf=BytesIO()
        with ZipFile(buf,'w') as z:z.writestr(filename,content)
        with pytest.raises(ValueError):parser.parse(buf.getvalue())
    buf=BytesIO()
    with ZipFile(buf,'w') as z:
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="2"><c r="A2" t="inlineStr"><is><t>Product</t></is></c><c r="B2"><f>1+2</f><v>3</v></c></row></sheetData></worksheet>')
    assert parser.parse(buf.getvalue())[0]['error']


def test_real_adapters_fail_closed(monkeypatch):
    from app.services.buying_adapters import DisabledGmailSupplierSender,DisabledMoySkladProcurementAdapter
    monkeypatch.setattr(settings,'external_writes_enabled',False)
    monkeypatch.setattr(settings,'supplier_email_send_enabled',False)
    async def run():
        with pytest.raises(ValueError):await DisabledGmailSupplierSender().send(key='x',recipient='x',body='x')
        with pytest.raises(ValueError):await DisabledMoySkladProcurementAdapter().order(1,1)
        with pytest.raises(ValueError):await DisabledMoySkladProcurementAdapter().receipt(1)
    asyncio.run(run())


def test_catalog_sort_pagination_manual_mapping_and_stale_import(workspace):
    c,factory,_=workspace;login(c)
    a,b=supplier(c),supplier(c)
    for sid,name,price in ((a,'Z expensive','50'),(b,'A inexpensive','1')):
        assert confirm(c,sid,preview(c,sid,[['Name','Price'],[name,price]])).status_code==200
    result=c.get('/buying/catalog?sort=cheapest&limit=1').json()
    assert result['total']==2 and result['products'][0]['name']=='A inexpensive'
    assert c.get('/buying/catalog?offset=1&limit=1').json()['products'][0]['name']=='Z expensive'
    assert c.get('/buying/catalog?q=%25').json()['total']==0
    for sort in ('supplier','newest'):
        assert c.get('/buying/catalog?sort='+sort).status_code==200
    # The first row is new; a later stale price must roll it back entirely.
    p=preview(c,a,[['Name','Price'],['New rollback row','7'],['Z expensive','60']])
    assert confirm(c,a,preview(c,a,[['Name','Price'],['Z expensive','55']])).status_code==200
    assert confirm(c,a,p).status_code==409
    assert c.get('/buying/catalog?q=rollback').json()['total']==0
    assert c.get('/buying/suppliers').json()['suppliers'][0]['active_offer_count']==1


def test_session_expiry_and_filename_validation(workspace):
    c,factory,_=workspace;login(c);sid=supplier(c)
    assert c.post(f'/buying/suppliers/{sid}/price-lists/preview?filename=../bad.xlsx',content=xlsx([['x','y']])).status_code==422
    assert c.post(f'/buying/suppliers/{sid}/price-lists/preview?filename=bad.xls',content=b'x').status_code==422
    from app.models.buying import BuyingSession
    from datetime import timedelta
    async def expire():
        async with factory() as s,s.begin():
            rows=(await s.scalars(select(BuyingSession))).all()
            for r in rows:r.expires_at=service.now()-timedelta(hours=1)
    asyncio.run(expire())
    assert c.get('/buying/cart').status_code==401
