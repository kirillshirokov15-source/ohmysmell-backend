"""Generate a complete frontend reference from explicit synthetic examples."""
import json
from pathlib import Path


def main():
    timestamp='2026-09-25T12:00:00Z'
    product='buying-00000000-0000-0000-0000-000000000001'
    upload='00000000-0000-0000-0000-000000000002'
    parser=dict(version='columns-v1',sheet=1,first_row=2,name_column='A',price_column='B',sku_column=None)
    supplier_input=dict(name='Synthetic supplier',email='supplier@example.invalid',currency='RUB',parser=parser)
    contact=dict(pickup_address='Test street 1',phone='+70000000000',pickup_notes='Call on arrival')
    supplier=dict(id=1,**supplier_input,status='active',latest_price_list_upload=timestamp,active_offer_count=1,created_at=timestamp,updated_at=timestamp,**contact)
    offer=dict(id=1,product_id=product,supplier_id=1,supplier_name=supplier['name'],name='Synthetic perfume 100 ml',supplier_sku=None,purchase_price_minor=1250,currency_code='RUB',approximate_rub_minor=1250,approximate=False,fx_source=None,fx_rate_date=None,fx_rate_to_rub=None,price_list_updated_at=timestamp,availability='on_request',active=True,moysklad_match_state='local')
    item=dict(offer_id=1,product_id=product,name=offer['name'],quantity=2,unit_price_minor=1250,approximate_rub_minor=1250,fx_rate_to_rub=None,fx_source=None,fx_rate_date=None)
    group=dict(supplier_id=1,supplier_name=supplier['name'],recipient=supplier['email'],items=[item],currency='RUB',total_minor=2500,approximate_rub_minor=2500,approximate=False,email_body='Synthetic perfume 100 ml – 2 шт.')
    purchase=dict(**group,id=1,number='B-000001',item_count=1,status='sent',send_state='simulated',created_at=timestamp,updated_at=timestamp,sent_at=timestamp,received_at=None,received_by_user_id=None,received_by_role=None,received_by_username=None,message_id='fake-message:1',thread_id='fake-thread:1',external_ids={'supplier_order':'fake-order:1','counterparty':'fake-supplier:1'})
    received={**purchase,'status':'received','received_at':timestamp,'received_by_user_id':2,'received_by_role':'picker','received_by_username':'buying_picker'}
    pickup=dict(id=1,number='B-000001',supplier_name=supplier['name'],**contact,items=[dict(name=item['name'],quantity=2)],sent_at=timestamp,status='sent',received_at=None,received_by_user_id=None,received_by_role=None,received_by_username=None)
    picked={**pickup,**{k:received[k] for k in ('status','received_at','received_by_user_id','received_by_role','received_by_username')}}
    counts=dict(total=1,valid=1,invalid=0,new=1,changed=0,unchanged=0,ambiguous=0)
    row=dict(row=2,name=offer['name'],sku=None,mapping_key='name:synthetic perfume 100 ml',price_minor=1250,error=None,offer_id=None,old_price_minor=None,product_id=None,candidates=[],state='new')
    preview=dict(import_id=upload,counts=counts,rows=[row],currency='RUB',parser_version='columns-v1')
    fingerprint='a'*64
    entries=[]
    def add(method,path,role,body,result,errors='',notes=''):
        entries.append((method,'/buying'+path,role,body,result,errors,notes))
    add('POST','/auth/login','anonymous',{'username':'buying_manager','password':'<entered password>'},dict(access_token='<opaque token>',token_type='bearer',expires_in=28800),'401, 422, 429, 503','No role field. Username is case-insensitive ASCII; password whitespace is significant. Limit: 30 attempts/minute/process; Retry-After: 60.')
    add('GET','/auth/me','manager or picker',None,dict(id=1,username='buying_manager',role='manager'))
    add('POST','/auth/logout','manager or picker',None,dict(revoked=True))
    add('GET','/catalog','manager',None,dict(products=[dict(id=product,name=offer['name'],offers=[offer])],total=1,offset=0,limit=50),notes='Query: q (partial name, max 200), sort=cheapest|supplier|newest, offset>=0, limit=1..100 (default 50). Canonical-product pagination. No automatic fuzzy selection.')
    add('GET','/products/{product_id}/offers','manager',None,dict(offers=[offer]),notes='Path: canonical product_id. Unknown product returns an empty offers list.')
    add('GET','/offers/{offer_id}/price-history','manager',None,dict(history=[dict(id=1,offer_id=1,old_price_minor=1000,new_price_minor=1250,currency='RUB',import_id=upload,changed_at=timestamp)]),'404', 'Query: offset>=0, limit=1..100.')
    add('POST','/offers/{offer_id}/estimate','manager',None,dict(offer_id=1,fx_rate_to_rub='90.0000000000',fx_source='cbr',fx_rate_date='2026-09-25'),'404, 503','Refreshes only a read-only FX quote. CBR is a reference/fallback rate, not a live bank selling rate.')
    add('GET','/cart','manager',None,dict(items=[dict(offer=offer,quantity=2,price_changed_since_added=False,created_at=timestamp,updated_at=timestamp)]))
    add('POST','/cart/items','manager',dict(offer_id=1,quantity=2),dict(offer_id=1,quantity=2),'404','Absolute quantity upsert, not increment. Strict integer 1..100000 pieces. Shared persistent cart.')
    add('PATCH','/cart/items/{offer_id}','manager',dict(quantity=3),dict(offer_id=1,quantity=3),'404')
    add('DELETE','/cart/items/{offer_id}','manager',None,dict(removed=True),notes='Repeat-safe, including absent item.')
    add('DELETE','/cart','manager',None,dict(cleared=True))
    add('POST','/checkout/preview','manager',None,dict(fingerprint=fingerprint,suppliers=[group]),'409','Empty cart, inactive/unavailable offers or missing supplier email: 409. Body contains product/quantity lines only; editing is unsupported.')
    add('POST','/checkout/confirm','manager',dict(fingerprint=fingerprint),dict(purchase_ids=[1],real_email_sent=False),'409','Required header Idempotency-Key: 8..100 characters. Save key BEFORE submitting; reuse same key and fingerprint after timeout. Stale cart/prices or conflicting key: 409. One draft per supplier; clears cart atomically. Does not send email.')
    add('GET','/purchases','manager',None,dict(purchases=[purchase],total=1,offset=0,limit=50),notes='Query: supplier_id optional, offset>=0, limit=1..100. Newest first. No date filter yet.')
    add('GET','/purchases/{purchase_id}','manager',None,{**purchase,'replies':[dict(message_id='synthetic-reply',thread_id='fake-thread-1',received_at=timestamp,subject='Re: order',body='Supplier text',attachments=[])]},'404','All reply bodies/subjects/attachment filenames are untrusted text. Render as text, never raw HTML.')
    add('POST','/purchases/{purchase_id}/received','manager',None,{**received,'received_by_user_id':1,'received_by_role':'manager','received_by_username':'buying_manager'},'404, 409','Only sent purchases; replay returns original received time/actor, no new audit event. Fake receipt only.')
    add('POST','/purchases/{purchase_id}/simulate-send','manager',None,purchase,'403, 404, 409','Staging/development only. Fake supplier email and Supplier Order hook. Never label this a real email.')
    add('GET','/suppliers','manager',None,dict(suppliers=[supplier],offset=0,limit=50),notes='Query: offset>=0, limit=1..100. One logical warehouse: Внешние поставщики.')
    add('POST','/suppliers','manager',supplier_input,dict(id=1))
    add('GET','/suppliers/{supplier_id}','manager',None,supplier,'404')
    add('PATCH','/suppliers/{supplier_id}/pickup','manager',contact,supplier,'404','Partial update: omitted values preserved; null clears a field. Limits: address 1000, phone 80, notes 2000 characters. Keep pickup notes free of commercial/customer data; pickers read them.')
    add('POST','/suppliers/{supplier_id}/configure','manager',supplier_input,dict(id=1),'404, 409','One-time adoption of an existing external supplier. Not a general update endpoint. Already configured/currency conflict: 409.')
    add('POST','/offers/{offer_id}/mapping','manager',dict(name=offer['name']),dict(offer_id=1,product_id=product),'404, 409','Adopt an existing supplier offer without duplicating its identity.')
    add('POST','/products/mappings','manager',dict(product_id=product,name=offer['name']),dict(product_id=product),'404, 409','Register an existing local supply identity before XLSX matching.')
    add('POST','/suppliers/{supplier_id}/price-lists/preview','manager','XLSX',preview,'404, 413, 422','Required query filename=synthetic.xlsx. Raw binary body, NOT multipart/JSON. Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet. Max 2 MiB, 5000 rows; macros/formulas not executed. Supplier-specific parser settings are used.')
    add('POST','/suppliers/{supplier_id}/price-lists/import','manager',dict(import_id=upload,mappings={}),dict(import_id=upload,imported_at=timestamp,counts=counts),'404, 409','For ambiguous rows supply mappings={"2":"listed-candidate-id"}. Invalid rows require corrected upload. Atomic, repeat-safe import; stale preview conflicts. Missing rows do not deactivate prior offers.')
    add('GET','/settings/status','manager',None,dict(gmail=dict(status='reauth_required',checked_at=timestamp,reauth_required=True),moysklad=dict(status='configured',writes_enabled=False),telegram=dict(status='not_configured'),fx=dict(status='cached',rate='90.0000000000',updated_at='2026-09-25',source='cbr'),supplier_email=dict(status='disabled')),notes='Gmail is worker heartbeat: connected, reauth_required, bad_credentials, network_unavailable, database_unavailable, worker_error, worker_stale or unknown. Stale heartbeat is not healthy. MoySklad configured means credentials present, not a live probe; Telegram configured means validated group configuration. FX cached is not a live quote; display date. No provider call is triggered by Settings.')
    for path,status,value in [('/picker/pickups','sent',pickup),('/picker/history','received',picked)]:
        add('GET',path,'manager or picker',None,dict(pickups=[value],total=1,offset=0,limit=50),notes=f'Only {status} purchases. Query: offset>=0, limit=1..100. All employees see the shared list; drafts/errors/cancellations excluded.')
    add('GET','/picker/pickups/{purchase_id}','manager or picker',None,pickup,'404','Only sent/received purchases are visible. Explicit response allowlist contains no prices, currency, email, replies or external IDs.')
    add('POST','/picker/pickups/{purchase_id}/received','manager or picker',None,picked,'404, 409','The Забрал action. Actor comes from authenticated account, never request JSON. Repeated action retains first actor/time and one event.')
    header='''# OhMySmell Buying: Sites API contract

Staging base URL: `https://ohmysmell-backend-staging-staging.up.railway.app`.
Exact Sites origin: `https://ohmysmell-buying.christiankvyatkovsky.chatgpt.site`.
Set `BUYING_ALLOWED_ORIGINS` to that origin; comma-separated explicit origins are supported.
No wildcard/cookies. Existing `CORS_ORIGINS` continues to govern non-Buying paths.

1. POST login with username/password; there is no role selector or shared-password fallback.
2. Keep access_token in SPA memory; send `Authorization: Bearer <token>` on every other endpoint.
3. GET `/buying/auth/me`: manager opens full Buying UI, picker opens pickup UI.
4. On 401 clear token and show login. On 403 show permission error; do not retry as another role.
5. POST logout revokes current session; clear local token. Expiry: 8 hours. Other sessions remain valid.

Role restrictions are enforced by backend dependencies, including direct requests. Account deactivation
and role changes take effect on the next request. Password reset CLI revokes all user sessions;
session-secret rotation revokes all sessions. Legacy anonymous-user sessions cannot authenticate.
Never put bearer tokens in URLs or store passwords. All Buying responses are non-cacheable.

Money: integer minor units (100 minor = 1 RUB/USD); FX rates: decimal strings, never floats.
USD RUB estimates may be null and are approximate; confirmed purchase snapshots are immutable.
No customer, margin, retail sale price or customer-order fields exist in this workspace.
All date/time values are ISO 8601 UTC; render in the user's local timezone.
Synthetic examples below do not represent live suppliers or credentials.

Machine-readable schemas: [openapi.json](openapi.json), also `/openapi.json` on staging.
Account setup: [BUYING_ROLES.md](BUYING_ROLES.md). XLSX layouts: [BUYING_PRICE_LISTS.md](BUYING_PRICE_LISTS.md).

## Errors and retries

All successful calls below return HTTP 200. All protected calls can return 401 (missing/expired/
revoked/inactive session), 403 (role), 422 (validation) or 503 (configuration/backend unavailable).
Entity/state-specific errors are listed per endpoint. 413 is possible for oversized bodies.
Example error: `{"detail":"Manager role required"}`. Validation envelope:
`{"detail":{"code":"validation_error","fields":[{"loc":["body","username"],"type":"missing"}]}}`.
Unexpected failures return a safe service_unavailable envelope, never provider details/stack traces.
The frontend must accept detail as either a string or an object.

Checkout uses an Idempotency-Key. Never generate a new key just because the response timed out.
Received and import replay are safe. Do not automatically retry supplier creation after an unknown outcome.
Preflight OPTIONS requires no token; unlisted browser origins receive no allow-origin header.
CORS is a browser policy; authorization is required even without Origin.

Real supplier email and MoySklad writes remain disabled. No live send endpoint exists.
Draft purchases are not pickup tasks until the explicit staging simulation marks them sent.
Readonly supplier reply polling is optional and requires working Gmail OAuth; no semantic confirmation parsing.
'''
    output=[header]
    for method,path,role,body,result,errors,notes in entries:
        auth='None' if role=='anonymous' else 'Bearer session'
        output.append(f'\n## {method} {path}\n\nAuth: {auth}. Allowed role: {role}.\n')
        output.append(('Request: none (no body).' if body is None else 'Request: raw XLSX bytes (no JSON).' if body=='XLSX' else 'Request JSON:\n```json\n'+json.dumps(body,ensure_ascii=False,indent=2)+'\n```')+'\n')
        output.append('Response JSON (200):\n```json\n'+json.dumps(result,ensure_ascii=False,indent=2)+'\n```\n')
        codes=errors if role=='anonymous' else ', '.join(dict.fromkeys(('401, 403, 422, 503'+(', '+errors if errors else '')).split(', ')))
        output.append(('Error status codes: '+codes+'. '+notes).rstrip()+'\n')
    Path('docs/BUYING_API.md').write_text('\n'.join(output),encoding='utf-8')
    # Fail when a new route has been omitted from the frontend reference.
    from app.api.buying import auth_router, router, picker_router
    actual={(m,r.path) for group in (auth_router,router,picker_router) for r in group.routes for m in r.methods}
    assert actual=={(e[0],e[1]) for e in entries}, actual ^ {(e[0],e[1]) for e in entries}
    print(f'Buying contract: {len(entries)} endpoints documented')


if __name__=='__main__': main()
