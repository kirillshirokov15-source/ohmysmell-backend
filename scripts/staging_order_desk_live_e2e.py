"""Explicitly approved synthetic staging session; encrypted rollback state, no PII output.

Only use after approval for the test user and the existing manager group. No token
changes, migrations, Gmail or warehouse operations. All variable values travel via stdin.
"""
import argparse
import json
import subprocess
import shutil
import traceback
from pathlib import Path
from uuid import uuid4
from datetime import datetime, timezone
from dotenv import dotenv_values
import psycopg2
from psycopg2.extras import RealDictCursor
from scripts.buying_staging_accounts import protect
from scripts.staging_order_desk_audit import cli, PROJECT, ENVIRONMENT, SERVICES, APPROVED_USER
from app.integrations.http_tls import verified_session

STATE = Path('.staging-artifacts/order-desk-approved-session.dpapi')
REPORT = Path('.staging-artifacts/order-desk-approved-session.json')
BASE = 'https://ohmysmell-backend-staging-staging.up.railway.app'
SCOPE = 'ORDER_DESK_STAGING_'
http = verified_session()


def save(state):
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_bytes(protect(json.dumps(state).encode()))


def load():
    return json.loads(protect(STATE.read_bytes(), decrypt=True))


def emit(event, **values):
    print(json.dumps({'event':event, **values}), flush=True)


def api(query, variables):
    result = subprocess.run([shutil.which('railway.cmd'), 'api', query, '--variables', '@-', '--compact'],
        input=json.dumps(variables), capture_output=True, text=True, encoding='utf-8', timeout=60)
    if result.returncode:
        raise RuntimeError('railway_api_failed')
    data = json.loads(result.stdout)
    if data.get('errors'):
        raise RuntimeError('railway_api_error')
    return data['data']


def target(state, role):
    return dict(projectId=PROJECT, environmentId=ENVIRONMENT, serviceId=state['services'][role]['id'])


def raw_variables(state, role):
    return api('query($projectId:String!,$environmentId:String!,$serviceId:String!){variables(projectId:$projectId,environmentId:$environmentId,serviceId:$serviceId,unrendered:true)}', target(state,role))['variables']


def rendered(state, role):
    return json.loads(cli('variable','list','--project',PROJECT,'--environment',ENVIRONMENT,
        '--service',state['services'][role]['id'],'--json'))


def verify(state):
    status=json.loads(cli('status','--json'))
    assert status['id']==PROJECT and status['name']=='eloquent-wisdom'
    env=next(e['node'] for e in status['environments']['edges'] if e['node']['id']==ENVIRONMENT)
    assert env['name']=='staging'
    instances={s['node']['serviceName']:s['node'] for s in env['serviceInstances']['edges']}
    for role,name in SERVICES.items():
        assert instances[name]['serviceId']==state['services'][role]['id']
    return instances


def db():
    values=dotenv_values('.env')
    connection=psycopg2.connect(values['STAGING_DATABASE_URL'],connect_timeout=10,
        options='-c statement_timeout=10000')
    connection.set_session(readonly=True)
    return connection


def rows(query, params=()):
    with db() as connection,connection.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(query,params)
        return [dict(r) for r in cursor.fetchall()]


def queue():
    return rows('SELECT id,draft_id,direction,destination,status,kind,sender_type,sender_telegram_id,attempts,telegram_message_id,error_code FROM desk_messages ORDER BY id')


def tg(token, method, data=None):
    result=http.post('https://api.telegram.org/bot'+token+'/'+method,json=data or {},timeout=25).json()
    if not result.get('ok'):
        raise RuntimeError('telegram_request_failed')
    return result['result']


def init():
    assert not STATE.exists(), 'session_already_exists'
    status=json.loads(cli('status','--json'))
    assert status['id']==PROJECT
    env=next(e['node'] for e in status['environments']['edges'] if e['node']['id']==ENVIRONMENT)
    assert env['name']=='staging'
    instances={s['node']['serviceName']:s['node'] for s in env['serviceInstances']['edges']}
    state={'run':'synthetic-'+uuid4().hex,'created_at':datetime.now(timezone.utc).isoformat(),
        'services':{role:{'id':instances[name]['serviceId'],'touched':[]} for role,name in SERVICES.items()},'drafts':{}}
    from sqlalchemy.engine import make_url
    local=dotenv_values('.env');stage=make_url(local['STAGING_DATABASE_URL'])
    identity=lambda u:(u.username,u.password,u.database)
    assert identity(stage)!=identity(make_url(local['DATABASE_URL']))
    for role,item in state['services'].items():
        v=rendered(state,role)
        assert v.get('APP_ENV')=='staging' and v.get('EXTERNAL_WRITES_ENABLED')=='false'
        assert v.get('ORDER_DESK_SEND_ENABLED','false')=='false'
        assert identity(make_url(v['DATABASE_URL']))==identity(stage)
        item['original']=raw_variables(state,role)
        item['rendered']=v
        assert not any(k.startswith('RAILWAY_') and k not in {'RAILWAY_DEPLOYMENT_DRAINING_SECONDS','RAILWAY_DEPLOYMENT_OVERLAP_SECONDS'}
                       for k in item['original']), 'raw_variables_include_provider_values'
    manager=state['services']['manager']['rendered'];client=state['services']['client']['rendered']
    group=int(manager['MANAGER_TELEGRAM_CHAT_ID']);assert group<0
    assert APPROVED_USER in {int(x.strip()) for x in manager['MANAGER_TELEGRAM_USER_IDS'].split(',')}
    assert rows('SELECT id FROM managers WHERE telegram_id=%s AND is_active=true',(APPROVED_USER,))
    chat=tg(manager['TELEGRAM_BOT_TOKEN'],'getChat',{'chat_id':group})
    assert chat['title']=='OhMySmell Work' and chat['type'] in ('group','supergroup')
    member=tg(manager['TELEGRAM_BOT_TOKEN'],'getChatMember',{'chat_id':group,'user_id':APPROVED_USER})
    assert member['status'] in ('member','administrator','creator')
    assert manager['TELEGRAM_BOT_TOKEN'].split(':')[0]!=client['CLIENT_TELEGRAM_BOT_TOKEN'].split(':')[0]
    state['group']=group;state['before_queue']=queue()
    assert not rows("SELECT id FROM draft_notifications WHERE status!='sent'")
    assert not rows('SELECT id FROM buying_events WHERE notified_at IS NULL')
    state['secret']=__import__('secrets').token_urlsafe(40)
    save(state)
    emit('session_saved_encrypted', test_user=APPROVED_USER, group_verified=True,
         queue_count=len(state['before_queue']), unknown_recipients=sum(r['destination'] not in (APPROVED_USER,group) for r in state['before_queue']))


def change(state,role,values):
    verify(state)
    v=rendered(state,role)
    assert v['APP_ENV']=='staging' and v['EXTERNAL_WRITES_ENABLED']=='false'
    allowed={'TILDA_ENABLED','TILDA_WEBHOOK_SECRET','TILDA_FIELD_MAP_JSON','TILDA_ITEM_FIELD_MAP_JSON','TILDA_PRODUCT_MAP_JSON',
        'CLIENT_TELEGRAM_BOT_USERNAME','MANAGER_TELEGRAM_CHAT_ID','MANAGER_TELEGRAM_USER_IDS','ORDER_DESK_SEND_ENABLED',
        SCOPE+'CLIENT_RECIPIENT_IDS',SCOPE+'MANAGER_CHAT_IDS',SCOPE+'MESSAGE_IDS',SCOPE+'DRAFT_IDS',SCOPE+'NOT_BEFORE'}
    assert set(values)<=allowed
    state['services'][role]['touched']=sorted(set(state['services'][role]['touched'])|set(values))
    save(state) # Recovery inventory precedes mutation.
    api('mutation($input:VariableCollectionUpsertInput!){variableCollectionUpsert(input:$input)}',
        {'input':{**target(state,role),'variables':values,'skipDeploys':True,'replace':False}})
    current=rendered(state,role)
    assert all(current.get(k)==v for k,v in values.items())
    emit('variables_saved',role=role,keys=sorted(values),sending=current.get('ORDER_DESK_SEND_ENABLED','false'))


def configure(state):
    mapping=json.dumps({'synthetic-a':{'product_id':state['run']+'-a','retail_price_minor':1010},
                        'synthetic-b':{'product_id':state['run']+'-b','retail_price_minor':1010}})
    routing={'MANAGER_TELEGRAM_CHAT_ID':str(state['group']),'MANAGER_TELEGRAM_USER_IDS':str(APPROVED_USER)}
    scope={SCOPE+'CLIENT_RECIPIENT_IDS':str(APPROVED_USER),SCOPE+'MANAGER_CHAT_IDS':str(state['group']),
           SCOPE+'MESSAGE_IDS':'',SCOPE+'DRAFT_IDS':'',SCOPE+'NOT_BEFORE':state['created_at'],'ORDER_DESK_SEND_ENABLED':'false'}
    for role in ('manager','client'):
        change(state,role,{**routing,**scope,**({'TILDA_PRODUCT_MAP_JSON':mapping} if role=='manager' else {})})
    change(state,'backend',{**routing,'TILDA_ENABLED':'true','TILDA_WEBHOOK_SECRET':state['secret'],
        'TILDA_FIELD_MAP_JSON':json.dumps({'external_id':'tranid','name':'Name','phone':'Phone','email':'Email','comment':'Comments','items':'products','total':'amount','currency':'currency','discount':'discount'}),
        'TILDA_ITEM_FIELD_MAP_JSON':json.dumps({'id':'externalid','name':'name','qty':'quantity','price':'price'}),
        'TILDA_PRODUCT_MAP_JSON':mapping,'CLIENT_TELEGRAM_BOT_USERNAME':'OhMySmell_OrdersBot'})


def deploy(state,role,upload=False):
    verify(state)
    v=rendered(state,role);assert v['EXTERNAL_WRITES_ENABLED']=='false'
    if role=='manager':
        assert not rows("SELECT id FROM draft_notifications WHERE status!='sent'")
        assert not rows('SELECT id FROM buying_events WHERE notified_at IS NULL')
    args=('--project',PROJECT,'--environment',ENVIRONMENT,'--service',state['services'][role]['id'])
    if upload:
        assert v.get('ORDER_DESK_SEND_ENABLED','false')=='false'
        from scripts.staging_order_desk_deploy import prepare_upload
        revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
        assert subprocess.check_output(['git','branch','--show-current'],text=True).strip()=='feature/sales-core-v2'
        path=prepare_upload(revision,role)
        if role=='manager':
            (path/'railway.toml').write_text('[build]\nbuilder="DOCKERFILE"\ndockerfilePath="Dockerfile"\n[deploy]\nstartCommand="python -m app.workers.manager_bot"\nhealthcheckPath="/health"\nhealthcheckTimeout=120\n',encoding='utf-8')
        cli('up',str(path),'--path-as-root','--no-gitignore',*args,'--detach','--message','approved synthetic '+revision)
    else:
        cli('redeploy',*args,'--yes','--json')
    emit('deploy_requested',role=role,upload=upload)


def observe(state):
    instances=verify(state);messages=queue()
    safe=[]
    for r in messages:
        safe.append({k:v for k,v in r.items() if k not in ('destination','sender_telegram_id') } |
            {'recipient':'test_user' if r['destination']==APPROVED_USER else 'approved_group' if r['destination']==state['group'] else 'UNAPPROVED',
             'actor_is_test_user':r['sender_telegram_id']==APPROVED_USER})
    drafts=rows('SELECT draft_id,order_id,manager_id,actor_telegram_id,customer_telegram_id,stage FROM order_desks WHERE external_id LIKE %s',(state['run']+'%',))
    for d in drafts:
        d['assigned_to_test_user']=d.pop('actor_telegram_id')==APPROVED_USER
        d['linked_to_test_user']=d.pop('customer_telegram_id')==APPROVED_USER
    events=rows('SELECT draft_id,action,actor_telegram_id FROM desk_events WHERE draft_id=ANY(%s) ORDER BY id',([d['draft_id'] for d in drafts],))
    for e in events:e['actor_is_test_user']=e.pop('actor_telegram_id')==APPROVED_USER
    order_ids=[d['order_id'] for d in drafts if d['order_id']]
    orders=rows('SELECT id,revision,payment_status,fulfillment_status,delivery_status,manual_fulfillment,customer_type,total,moysklad_order_id FROM orders WHERE id=ANY(%s)',(order_ids,))
    report={'services':{r:{'status':instances[n]['latestDeployment']['status'],'deployment_id':instances[n]['latestDeployment']['id']} for r,n in SERVICES.items()},
        'messages':safe,'drafts':drafts,'events':events,'orders':orders,
        'external_operations':rows('SELECT count(*) AS count FROM external_operations')[0]['count']}
    REPORT.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)


def create(state):
    v=rendered(state,'backend');assert v['TILDA_ENABLED']=='true' and v['TILDA_WEBHOOK_SECRET']==state['secret']
    headers={'X-Tilda-Secret':state['secret']}
    payload={'tranid':state['run'],'Name':'SYNTHETIC E2E TEST','Comments':'Synthetic staging only; manual fulfillment',
        'currency':'RUB','amount':'30.30','products':[{'externalid':'synthetic-a','name':'SYNTHETIC A','quantity':'2','price':'10.10'},
        {'externalid':'synthetic-b','name':'SYNTHETIC B','quantity':'1','price':'10.10'}]}
    for label,total in (('valid','30.30'),('mismatch','30.31')):
        data={**payload,'tranid':state['run']+('-review' if label=='mismatch' else ''),'amount':total}
        # Real HTTPS form-urlencoded intake; products is a JSON string.
        response=http.post(BASE+'/integrations/tilda/orders',headers=headers,data={**data,'products':json.dumps(data['products'])},timeout=20)
        assert response.status_code in (200,202), 'synthetic_intake_failed'
        result=response.json();state['drafts'][label]=result['draft_id'];save(state)
        repeated=http.post(BASE+'/integrations/tilda/orders',headers=headers,json=data,timeout=20)
        assert repeated.status_code==200 and repeated.json()['draft_id']==result['draft_id'] and repeated.json()['duplicate']
        emit('synthetic_intake',label=label,draft_id=result['draft_id'],first_status=response.status_code,repeat_status=repeated.status_code)
    did=state['drafts']['valid']
    checked=rows('SELECT id,customer_type,total,status FROM draft_orders WHERE id=ANY(%s)',(list(state['drafts'].values()),))
    assert all(d['customer_type']=='retail' for d in checked)
    assert next(d for d in checked if d['id']==did)['total']==3030
    assert next(d for d in checked if d['id']==state['drafts']['mismatch'])['total'] is None
    response=http.post(BASE+f'/internal/tilda/drafts/{did}/telegram-link',headers={'X-Internal-API-Token':v['INTERNAL_API_TOKEN']},timeout=20)
    assert response.status_code==200
    state['link']=response.json()['deep_link'];save(state)
    # Reserve an auditable scoped delivery. Never persist a plaintext link/token
    # in outbox body: the operator delivers it from encrypted session state.
    connection=db();connection.set_session(readonly=False)
    with connection,connection.cursor() as cursor:
        cursor.execute('''INSERT INTO desk_messages(draft_id,idempotency_key,direction,destination,body,sender_type,status,available_at)
            VALUES (%s,%s,'to_customer',%s,%s,'system','sending',now()+interval '20 minutes')
            ON CONFLICT(idempotency_key) DO NOTHING''',
            (did,state['run']+':test-link',APPROVED_USER,
             'SYNTHETIC one-time link delivery; token intentionally not persisted.'))
    connection.close()
    emit('synthetic_verified',draft_ids=state['drafts'],retail=True,mismatch_review=True,link_delivery_reserved=True)


def deliver_link(state):
    v=rendered(state,'client');did=state['drafts']['valid']
    assert v['EXTERNAL_WRITES_ENABLED']=='false' and v['ORDER_DESK_SEND_ENABLED']=='true'
    assert v[SCOPE+'CLIENT_RECIPIENT_IDS']==str(APPROVED_USER)
    assert did in {int(x) for x in v[SCOPE+'DRAFT_IDS'].split(',')}
    record=rows('SELECT id,status,attempts,destination,draft_id FROM desk_messages WHERE idempotency_key=%s',(state.get('link_delivery_key',state['run']+':test-link'),))[0]
    assert record['destination']==APPROVED_USER and record['draft_id']==did
    assert record['status']=='sending' and record['attempts']==0
    connection=db();connection.set_session(readonly=False)
    with connection,connection.cursor() as cursor:
        cursor.execute("UPDATE desk_messages SET attempts=1 WHERE id=%s AND status='sending' AND attempts=0 RETURNING id",(record['id'],))
        assert cursor.fetchone()
    body=f'SYNTHETIC staging test. Заявка №{did}. Откройте ссылку и нажмите Start:\n'+state['link']+f'\nЗатем отправьте /message {did} SYNTHETIC CLIENT E2E\nВ OhMySmell Work нажмите «Взять заказ» на карточке этой заявки.'
    try:
        sent=tg(v['CLIENT_TELEGRAM_BOT_TOKEN'],'sendMessage',{'chat_id':APPROVED_USER,'text':body,'protect_content':True,'disable_web_page_preview':True})
    except Exception:
        with connection,connection.cursor() as cursor:
            cursor.execute("UPDATE desk_messages SET status='uncertain',error_code='test_link_unknown_outcome' WHERE id=%s",(record['id'],))
        raise
    else:
        with connection,connection.cursor() as cursor:
            cursor.execute("UPDATE desk_messages SET status='sent',telegram_message_id=%s,sent_at=now() WHERE id=%s",(sent['message_id'],record['id']))
        emit('test_link_delivered',message_id=record['id'],telegram_message_id=sent['message_id'],recipient=APPROVED_USER)
    finally:connection.close()


def enable(state):
    messages=queue();dids=set(state['drafts'].values());assert dids
    scoped=[r for r in messages if r['draft_id'] in dids]
    assert all(r['destination'] in (APPROVED_USER,state['group']) for r in scoped)
    assert all(r['destination']==(APPROVED_USER if r['direction']=='to_customer' else state['group']) for r in scoped)
    # Only the audited original /start ack may be sent outside synthetic drafts.
    old=next((r for r in messages if r['id']==1),None)
    assert old and old['destination']==APPROVED_USER and old['draft_id'] is None and old['direction']=='to_customer'
    emit('scope_verified_before_enabling',test_user=APPROVED_USER,group_verified=True,
         message_ids=[1],draft_ids=sorted(dids),existing_scoped_message_ids=[r['id'] for r in scoped])
    for role in ('manager','client'):
        v=rendered(state,role)
        assert v[SCOPE+'CLIENT_RECIPIENT_IDS']==str(APPROVED_USER) and v[SCOPE+'MANAGER_CHAT_IDS']==str(state['group'])
        assert v['MANAGER_TELEGRAM_USER_IDS']==str(APPROVED_USER)
        change(state,role,{SCOPE+'MESSAGE_IDS':'1',SCOPE+'DRAFT_IDS':','.join(map(str,sorted(dids))),'ORDER_DESK_SEND_ENABLED':'true'})


def restore(state):
    # Disable first, deploy this safe intermediate state, then restore exact keys.
    for role in ('manager','client'):
        change(state,role,{'ORDER_DESK_SEND_ENABLED':'false'})
    change(state,'backend',{'TILDA_ENABLED':'false'})
    for role in ('manager','client','backend'):deploy(state,role)
    emit('shutdown_deploys_requested')


def restore_exact(state):
    verify(state)
    for role,item in state['services'].items():
        current=raw_variables(state,role);original=item['original'];touched=set(item['touched'])
        assert {k:v for k,v in current.items() if k not in touched}=={k:v for k,v in original.items() if k not in touched}, 'unrelated_variables_changed'
        restored={k:v for k,v in current.items() if k not in touched}
        restored.update({k:v for k,v in original.items() if k in touched})
        api('mutation($input:VariableCollectionUpsertInput!){variableCollectionUpsert(input:$input)}',
            {'input':{**target(state,role),'variables':restored,'skipDeploys':True,'replace':True}})
        assert raw_variables(state,role)==original
        emit('original_variables_restored',role=role,exact_match=True)
    for role in ('backend','manager','client'):deploy(state,role)
    state['restored_at']=datetime.now(timezone.utc).isoformat();save(state)


def verify_final(state):
    instances=verify(state);report={'services':{},'checked_at':datetime.now(timezone.utc).isoformat()}
    assert state.get('restored_at')
    for role,item in state['services'].items():
        exact=raw_variables(state,role)==item['original'];assert exact
        v=rendered(state,role)
        assert v['EXTERNAL_WRITES_ENABLED']=='false'
        assert v.get('ORDER_DESK_SEND_ENABLED','false')=='false'
        assert v.get('TILDA_ENABLED','false')=='false'
        deployment=instances[SERVICES[role]]['latestDeployment']
        assert deployment['status']=='SUCCESS'
        report['services'][role]={'variables_match_original':exact,'sending':False,'intake':False,
            'external_writes':False,'deployment_id':deployment['id'],'deployment_status':deployment['status']}
        if role in ('manager','client'):
            output=cli('logs','--project',PROJECT,'--environment',ENVIRONMENT,'--service',item['id'],'--lines','100','--json')
            messages=[json.loads(line).get('message','') for line in output.splitlines() if line.strip()]
            report['services'][role]['worker_ready_seen']=any('worker_ready' in x for x in messages)
            report['services'][role]['startup_errors']=sum('worker_startup_failed' in x for x in messages)
            assert report['services'][role]['worker_ready_seen'] and not report['services'][role]['startup_errors']
    response=http.post(BASE+'/integrations/tilda/orders',json={},timeout=15)
    assert response.status_code==503
    report['intake_http_status']=response.status_code
    report['queue_counts']=rows('SELECT status,count(*) AS count FROM desk_messages GROUP BY status')
    report['alembic']=rows('SELECT version_num FROM alembic_version')
    report['external_operations']=rows('SELECT count(*) AS count FROM external_operations')[0]['count']
    report['unapproved_recipients']=sum(r['destination'] not in (APPROVED_USER,state['group']) for r in queue())
    report['drafts']=rows('SELECT draft_id,order_id,manager_id,customer_telegram_id IS NOT NULL AS linked,stage FROM order_desks WHERE draft_id=ANY(%s)',(list(state['drafts'].values()),))
    Path('.staging-artifacts/order-desk-approved-final-verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=('init','configure','deploy','observe','create','enable','deliver-link','restore','restore-exact','verify-final'))
    parser.add_argument('--role',choices=tuple(SERVICES));parser.add_argument('--upload',action='store_true');args=parser.parse_args()
    if args.action=='init':return init()
    state=load()
    if args.action=='deploy':return deploy(state,args.role,args.upload)
    return {'configure':configure,'observe':observe,'create':create,'enable':enable,'deliver-link':deliver_link,'restore':restore,'restore-exact':restore_exact,'verify-final':verify_final}[args.action](state)


if __name__=='__main__':
    try:main()
    except Exception as error:
        frame=traceback.extract_tb(error.__traceback__)[-1]
        emit('operation_failed',category=type(error).__name__,function=frame.name,line=frame.lineno)
        raise SystemExit(1)
