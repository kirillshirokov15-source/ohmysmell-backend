"""Staging-only role, CORS and receiving acceptance. No real external writes."""
import asyncio
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from dotenv import load_dotenv
from scripts.buying_staging_accounts import accounts
from scripts.buying_staging_e2e import BASE
from app.integrations.http_tls import verified_session

SITE='https://ohmysmell-buying.christiankvyatkovsky.chatgpt.site'


def main():
    load_dotenv()
    from app.bot.staging_runner import validate_staging_config, activate_staging_config
    activate_staging_config(validate_staging_config(dict(os.environ)))
    from app.database.session import async_session, engine
    from app.models.supply import Supplier
    from app.models.buying import BuyingSupplier, BuyingPurchase, BuyingEvent
    from app.services.buying import now
    from sqlalchemy import select, func
    async def seed():
        async with async_session() as s, s.begin():
            supplier=Supplier(name='SYNTHETIC ROLE ACCEPTANCE '+uuid4().hex,supplier_type='external_wholesaler',email='test@example.invalid')
            s.add(supplier); await s.flush()
            s.add(BuyingSupplier(supplier_id=supplier.id,currency='RUB',parser={},pickup_address='Synthetic address',phone='+70000000000'))
            snapshot=dict(supplier_id=supplier.id,supplier_name=supplier.name,recipient=supplier.email,currency='RUB',total_minor=2000,approximate_rub_minor=2000,approximate=False,email_body='SYNTHETIC item – 2 шт.',items=[dict(offer_id=0,product_id='synthetic',name='SYNTHETIC item',quantity=2,unit_price_minor=1000,approximate_rub_minor=1000,fx_rate_to_rub=None,fx_source=None,fx_rate_date=None)])
            p=BuyingPurchase(supplier_id=supplier.id,snapshot=snapshot,status='sent',send_state='simulated',sent_at=now(),external_ids={})
            s.add(p); await s.flush()
            pid=p.id
        await engine.dispose()
        return pid
    pid=asyncio.run(seed())
    timings={}; creds=accounts(); report={'base':BASE,'origin':SITE,'purchase_id':pid}
    with verified_session() as http:
        def call(method,path,expected=200,**kwargs):
            started=perf_counter()
            r=http.request(method,BASE+'/buying'+path,timeout=(10,30),**kwargs)
            timings.setdefault(method+' '+path,[]).append(round((perf_counter()-started)*1000,2))
            if r.status_code!=expected:raise RuntimeError(f'HTTP acceptance: {method} {path} returned {r.status_code}, expected {expected}')
            return r
        for origin,status in ((SITE,200),('https://random.example',400)):
            r=call('OPTIONS','/auth/login',expected=status,headers={'Origin':origin,'Access-Control-Request-Method':'POST','Access-Control-Request-Headers':'content-type,authorization'})
            assert r.headers.get('Access-Control-Allow-Origin')==(origin if status==200 else None)
        report['cors']='passed'
        for role in ('manager','picker'):
            r=call('POST','/auth/login',json=creds[role],headers={'Origin':SITE})
            assert r.headers.get('Access-Control-Allow-Origin')==SITE
            http.headers['Authorization']='Bearer '+r.json()['access_token']
            me=call('GET','/auth/me').json()
            assert me['role']==role and me['username']==creds[role]['username']
            if role=='manager':
                call('GET','/catalog?limit=10')
                call('GET',f'/purchases/{pid}')
                report['integrations']=call('GET','/settings/status').json()
            else:
                for path in ('/catalog','/cart','/suppliers','/settings/status','/purchases'):
                    call('GET',path,expected=403)
                call('POST','/checkout/preview',expected=403)
                call('GET','/picker/pickups?limit=10')
                detail=call('GET',f'/picker/pickups/{pid}').json()
                assert detail['items']==[{'name':'SYNTHETIC item','quantity':2}]
                assert not any(k in json.dumps(detail) for k in ('price','currency','recipient','email','replies','external_ids'))
                first=call('POST',f'/picker/pickups/{pid}/received').json()
                second=call('POST',f'/picker/pickups/{pid}/received').json()
                assert first==second and first['received_by_user_id']==me['id'] and first['received_by_role']=='picker'
                call('GET','/picker/history')
                report['received_actor']=me['username']
            call('POST','/auth/logout')
            call('GET','/auth/me',expected=401)
            http.headers.pop('Authorization',None)
        report['roles']='passed'
    async def audit():
        async with async_session() as s:
            assert await s.scalar(select(func.count()).select_from(BuyingEvent).where(BuyingEvent.purchase_id==pid,BuyingEvent.action=='received'))==1
        await engine.dispose()
    asyncio.run(audit())
    report.update(audit='passed',timings_ms=timings,real_supplier_email_sent=False,real_moysklad_write=False)
    Path('.staging-artifacts/buying-roles-http.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    try: main()
    except Exception as error:
        print('Staging roles acceptance failed: '+type(error).__name__)
        raise SystemExit(1)
