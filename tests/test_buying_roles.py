import asyncio
import pytest
from sqlalchemy import select, func
from app.api import buying as api
from app.models.buying import BuyingUser, BuyingSession, BuyingEvent
from app.services.buying_passwords import hash_password, verify_password
from tests.test_buying import workspace, login, supplier, preview, confirm


@pytest.mark.parametrize('role', ['manager', 'picker'])
def test_login_me_logout(workspace, role):
    c, _, _ = workspace
    login(c, role)
    me = c.get('/buying/auth/me').json()
    assert set(me) == {'id', 'username', 'role'} and me['role'] == role
    assert c.post('/buying/auth/logout').status_code == 200
    assert c.get('/buying/auth/me').status_code == 401


@pytest.mark.parametrize('method,path', [
    ('get','/catalog'), ('get','/products/any/offers'), ('get','/offers/1/price-history'),
    ('get','/suppliers'), ('post','/suppliers/1/price-lists/preview?filename=test.xlsx'),
    ('get','/settings/status'), ('get','/cart'), ('post','/cart/items'),
    ('post','/checkout/preview'), ('post','/checkout/confirm'), ('get','/purchases'),
    ('get','/purchases/1'), ('post','/purchases/1/received'), ('patch','/suppliers/1/pickup')])
def test_picker_forbidden(workspace, method, path):
    c, _, _ = workspace
    login(c, 'picker')
    assert getattr(c, method)('/buying' + path).status_code == 403


def test_inactive_legacy_and_role_change(workspace):
    c, factory, _ = workspace
    assert c.post('/buying/auth/login',json={'password':'synthetic-password-only'}).status_code == 422
    login(c)
    async def deactivate():
        async with factory() as s, s.begin():
            user = await s.scalar(select(BuyingUser).where(BuyingUser.username == 'manager'))
            user.is_active = False
    asyncio.run(deactivate())
    assert c.get('/buying/cart').status_code == 401
    assert c.post('/buying/auth/login',json={'username':'manager','password':'synthetic-password-only'}).status_code == 401
    async def legacy():
        from datetime import timedelta
        async with factory() as s, s.begin():
            s.add(BuyingSession(digest=api.digest('legacy'), expires_at=api.service.now()+timedelta(hours=8)))
    asyncio.run(legacy())
    c.headers['Authorization']='Bearer legacy'
    assert c.get('/buying/cart').status_code == 401


@pytest.mark.parametrize('receiver', ['picker', 'manager'])
def test_pickup_allowlist_actor_and_replay(workspace, receiver):
    c, factory, _ = workspace
    login(c)
    sid = supplier(c)
    assert c.patch(f'/buying/suppliers/{sid}/pickup',json={'pickup_address':'Test street','phone':'+70000000000','pickup_notes':'Call on arrival'}).status_code == 200
    assert confirm(c,sid,preview(c,sid,[['Name','Price'],['Synthetic item','10']])).status_code == 200
    oid=c.get('/buying/catalog').json()['products'][0]['offers'][0]['id']
    c.post('/buying/cart/items',json={'offer_id':oid,'quantity':2})
    fp=c.post('/buying/checkout/preview').json()['fingerprint']
    pid=c.post('/buying/checkout/confirm',json={'fingerprint':fp},headers={'Idempotency-Key':'roles-checkout'}).json()['purchase_ids'][0]
    login(c,'picker')
    assert c.get(f'/buying/picker/pickups/{pid}').status_code == 404
    login(c)
    c.post(f'/buying/purchases/{pid}/simulate-send')
    login(c,receiver)
    data=c.get('/buying/picker/pickups').json()
    assert data['total']==1
    detail=c.get(f'/buying/picker/pickups/{pid}').json()
    assert set(detail)==set(api.PickupRead.model_fields)
    assert detail['items']==[{'name':'Synthetic item','quantity':2}]
    assert detail['pickup_address']=='Test street'
    assert not any(k in str(detail) for k in ('price','currency','recipient','email','rub','replies','snapshot'))
    route=f'/buying/picker/pickups/{pid}/received' if receiver=='picker' else f'/buying/purchases/{pid}/received'
    received=c.post(route).json()
    assert received['received_by_username']==receiver and received['received_by_role']==receiver
    first=received['received_at']
    login(c,'manager' if receiver=='picker' else 'picker')
    replay=c.post(f'/buying/picker/pickups/{pid}/received').json()
    assert replay['received_at']==first and replay['received_by_username']==receiver
    assert c.get('/buying/picker/pickups').json()['total']==0
    assert c.get('/buying/picker/history').json()['total']==1
    async def audit():
        async with factory() as s:
            events=(await s.scalars(select(BuyingEvent).where(BuyingEvent.action=='received'))).all()
            assert len(events)==1 and events[0].actor==f"user:{received['received_by_user_id']}"
    asyncio.run(audit())


def test_password_hashes():
    a,b=hash_password('synthetic-password-only'),hash_password('synthetic-password-only')
    assert a!=b and verify_password('synthetic-password-only',a)
    assert not verify_password('wrong',a) and not verify_password('wrong',None)


def test_sites_cors(monkeypatch):
    from app.api.buying_cors import BuyingCORSMiddleware
    from fastapi import FastAPI
    from tests.asgi_client import request
    site='https://ohmysmell-buying.christiankvyatkovsky.chatgpt.site'
    app=FastAPI()
    app.add_middleware(BuyingCORSMiddleware,buying_origins=[site],allow_origins=['https://existing.example'],allow_methods=['GET','POST'],allow_headers=['Authorization','Content-Type'])
    for origin, path, expected in [(site,'/buying/auth/login',200),('https://random.example','/buying/cart',400),(site,'/public/catalog',400),('https://existing.example','/public/catalog',200)]:
        status,_,headers=asyncio.run(request(app,'OPTIONS',path,headers={'Origin':origin,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'authorization,content-type'}))
        assert status==expected
        assert headers.get(b'access-control-allow-origin')==(origin.encode() if expected==200 else None)
        assert headers.get(b'access-control-allow-origin')!=b'*'
