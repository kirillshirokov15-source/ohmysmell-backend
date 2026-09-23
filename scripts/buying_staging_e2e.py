"""Synthetic staging-only HTTP acceptance; no supplier emails or MoySklad calls."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4
from dotenv import load_dotenv
from app.bot.staging_runner import validate_staging_config, activate_staging_config
from app.integrations.http_tls import verified_session

BASE = 'https://ohmysmell-backend-staging-staging.up.railway.app'
ARTIFACT = Path('.staging-artifacts/buying-http-e2e.json')


def main():
    args=argparse.ArgumentParser()
    args.add_argument('--verify',action='store_true')
    verify=args.parse_args().verify
    load_dotenv()
    activate_staging_config(validate_staging_config(dict(os.environ)))
    from tests.test_buying import xlsx
    timings={}
    with verified_session() as http:
        def request(method,path,**kwargs):
            started=perf_counter()
            response=http.request(method,BASE+'/buying'+path,timeout=(10,30),**kwargs)
            timings.setdefault(path.split('?')[0],[]).append(round((perf_counter()-started)*1000,2))
            if response.status_code!=200:
                raise RuntimeError(f'Staging HTTP {response.status_code} at {path.split("?")[0]}')
            return response.json()
        login=request('POST','/auth/login',json={'password':os.environ['BUYING_SHARED_PASSWORD']})
        http.headers['Authorization']='Bearer '+login['access_token']
        if verify:
            state=json.loads(ARTIFACT.read_text(encoding='utf-8'))
            assert state['base']==BASE
            for pid in state['purchase_ids']:
                purchase=request('GET',f'/purchases/{pid}')
                assert purchase['status']=='received' and purchase['send_state']=='simulated'
                assert purchase['supplier_name'].startswith('SYNTHETIC BUYING ')
                assert request('POST',f'/purchases/{pid}/received')['received_at']==purchase['received_at']
                assert request('POST',f'/purchases/{pid}/simulate-send')['message_id']==purchase['message_id']
            assert request('POST','/checkout/confirm',json={'fingerprint':state['fingerprint']},headers={'Idempotency-Key':state['key']})['purchase_ids']==state['purchase_ids']
            cart=request('GET','/cart')['items']
            assert any(i['offer']['id']==state['resume_offer'] and i['quantity']==4 for i in cart)
            request('DELETE',f'/cart/items/{state["resume_offer"]}')
            state['restart_verified']=True
        else:
            assert request('GET','/cart')['items']==[], 'Do not modify another manager cart'
            uid=uuid4().hex
            suppliers=[]
            for n in ('A','B'):
                sid=request('POST','/suppliers',json={'name':'SYNTHETIC BUYING '+uid+n,'email':'synthetic@example.invalid','currency':'RUB',
                    'parser':{'version':'columns-v1','sheet':1,'first_row':2,'name_column':'A','price_column':'B'}})['id']
                suppliers.append(sid)
                preview=request('POST',f'/suppliers/{sid}/price-lists/preview?filename=synthetic.xlsx',data=xlsx([['Name','Price'],['SYNTHETIC BUYING '+uid,'12.50']]),headers={'Content-Type':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'})
                request('POST',f'/suppliers/{sid}/price-lists/import',json={'import_id':preview['import_id']})
            catalog=request('GET','/catalog?q='+uid)
            assert catalog['total']==1 and len(catalog['products'][0]['offers'])==2
            offers=catalog['products'][0]['offers']
            for offer in offers:
                request('POST','/cart/items',json={'offer_id':offer['id'],'quantity':2})
            preview=request('POST','/checkout/preview')
            assert all(group['email_body'].endswith(' – 2 шт.') for group in preview['suppliers'])
            result=request('POST','/checkout/confirm',json={'fingerprint':preview['fingerprint']},headers={'Idempotency-Key':uid})
            assert len(result['purchase_ids'])==2 and not result['real_email_sent']
            threads=[]
            for pid in result['purchase_ids']:
                p=request('POST',f'/purchases/{pid}/simulate-send')
                assert p['message_id'].startswith('fake-')
                threads.append(p['thread_id'])
                request('POST',f'/purchases/{pid}/received')
            async def reply():
                from app.database.session import async_session,engine
                from app.services.buying import ingest_reply,now
                async with async_session() as session,session.begin():
                    args=dict(message_id='synthetic-'+uid,thread_id=threads[0],sender='synthetic@example.invalid',received_at=now(),subject='SYNTHETIC reply',body='SYNTHETIC availability question')
                    await ingest_reply(session,**args)
                    await ingest_reply(session,**args)
                await engine.dispose()
            asyncio.run(reply())
            assert len(request('GET',f'/purchases/{result["purchase_ids"][0]}')['replies'])==1
            request('POST','/cart/items',json={'offer_id':offers[0]['id'],'quantity':4})
            state=dict(base=BASE,key=uid,fingerprint=preview['fingerprint'],purchase_ids=result['purchase_ids'],resume_offer=offers[0]['id'],restart_verified=False)
        request('POST','/auth/logout')
    state['http_timings_ms']=timings
    ARTIFACT.write_text(json.dumps(state,indent=2),encoding='utf-8')
    print(json.dumps({'synthetic_buying_e2e':'passed','restart_verified':state['restart_verified'],'real_email_sent':False,'real_moysklad_write':False,'timings_ms':timings}))


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print('Buying staging E2E failed: '+type(error).__name__)
        raise SystemExit(1)
