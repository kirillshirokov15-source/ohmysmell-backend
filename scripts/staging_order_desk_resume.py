"""Resume explicitly approved Telegram acceptance on synthetic draft 36 only."""
import argparse
import asyncio
import hashlib
import os
import traceback
from uuid import uuid4
from datetime import datetime, timezone
from dotenv import dotenv_values
from scripts.staging_order_desk_live_e2e import (
    load, save, verify, rows, queue, rendered, raw_variables, change, emit, tg,
    http, db, STATE, BASE, SCOPE, APPROVED_USER)

DRAFT = 36


def draft(state):
    record=rows('SELECT draft_id,external_id,actor_telegram_id,customer_telegram_id,order_id FROM order_desks WHERE draft_id=%s',(DRAFT,))[0]
    assert record['external_id']==state['run']
    assert record['actor_telegram_id'] in (None,APPROVED_USER)
    assert record['customer_telegram_id'] in (None,APPROVED_USER)
    return record


def begin(state):
    verify(state);draft(state)
    for role,item in state['services'].items():
        assert raw_variables(state,role)==item['original'], 'baseline_changed'
        v=rendered(state,role)
        assert v['APP_ENV']=='staging' and v['EXTERNAL_WRITES_ENABLED']=='false'
        assert v.get('ORDER_DESK_SEND_ENABLED','false')=='false'
    manager=rendered(state,'manager')
    assert int(manager['MANAGER_TELEGRAM_CHAT_ID'])==state['group']
    assert APPROVED_USER in {int(x) for x in manager['MANAGER_TELEGRAM_USER_IDS'].split(',')}
    assert rows('SELECT id FROM managers WHERE telegram_id=%s AND is_active=true',(APPROVED_USER,))
    chat=tg(manager['TELEGRAM_BOT_TOKEN'],'getChat',{'chat_id':state['group']})
    assert chat['title']=='OhMySmell Work'
    member=tg(manager['TELEGRAM_BOT_TOKEN'],'getChatMember',{'chat_id':state['group'],'user_id':APPROVED_USER})
    assert member['status'] in ('member','administrator','creator')
    backup=STATE.with_name('order-desk-session-before-resume-'+uuid4().hex+'.dpapi')
    backup.write_bytes(STATE.read_bytes())
    state['resume_id']=uuid4().hex
    state['resume_started_at']=datetime.now(timezone.utc).isoformat()
    state['active_drafts']=[DRAFT]
    state.pop('restored_at',None)
    save(state)
    scope={SCOPE+'CLIENT_RECIPIENT_IDS':str(APPROVED_USER),SCOPE+'MANAGER_CHAT_IDS':str(state['group']),
        SCOPE+'DRAFT_IDS':str(DRAFT),SCOPE+'MESSAGE_IDS':'','ORDER_DESK_SEND_ENABLED':'false',
        'MANAGER_TELEGRAM_CHAT_ID':str(state['group']),'MANAGER_TELEGRAM_USER_IDS':str(APPROVED_USER)}
    for role in ('manager','client'):change(state,role,scope)
    change(state,'backend',{'CLIENT_TELEGRAM_BOT_USERNAME':'OhMySmell_OrdersBot','TILDA_ENABLED':'false'})
    emit('resume_prepared',draft_id=DRAFT,test_user=APPROVED_USER,intake=False,sending=False)


def link(state):
    record=draft(state)
    if record['customer_telegram_id']==APPROVED_USER:
        emit('already_linked',draft_id=DRAFT);return
    token=state['link'].split('start=',1)[1]
    previous=rows('SELECT expires_at>now() AS valid,expires_at>now()+interval \'15 minutes\' AS enough_time,consumed_by IS NOT NULL AS consumed FROM order_links WHERE token_hash=%s AND draft_id=%s',
        (hashlib.sha256(token.encode()).hexdigest(),DRAFT))
    emit('previous_link_checked',draft_id=DRAFT,valid=bool(previous and previous[0]['valid']),consumed=bool(previous and previous[0]['consumed']))
    if not previous or not previous[0]['enough_time'] or previous[0]['consumed']:
        v=rendered(state,'backend')
        response=http.post(BASE+f'/internal/tilda/drafts/{DRAFT}/telegram-link',
            headers={'X-Internal-API-Token':v['INTERNAL_API_TOKEN']},timeout=20)
        assert response.status_code==200
        state['link']=response.json()['deep_link']
        emit('link_renewed_for_existing_draft',draft_id=DRAFT)
    key=state['run']+':resume-link:'+state['resume_id']
    state['link_delivery_key']=key;save(state)
    connection=db();connection.set_session(readonly=False)
    with connection,connection.cursor() as cursor:
        cursor.execute('''INSERT INTO desk_messages(draft_id,idempotency_key,direction,destination,body,sender_type,status,available_at)
            VALUES (%s,%s,'to_customer',%s,%s,'system','sending',now()+interval '20 minutes')
            ON CONFLICT(idempotency_key) DO NOTHING''',
            (DRAFT,key,APPROVED_USER,'SYNTHETIC resumed one-time link delivery; token not persisted.'))
    connection.close()
    message=rows('SELECT id FROM desk_messages WHERE idempotency_key=%s',(key,))[0]
    emit('link_delivery_reserved',draft_id=DRAFT,message_id=message['id'])


def card(state):
    record=draft(state);v=rendered(state,'manager')
    original=rows("SELECT destination,telegram_message_id FROM desk_messages WHERE draft_id=%s AND kind='card' AND status='sent' ORDER BY id LIMIT 1",(DRAFT,))[0]
    assert original['destination']==state['group']==int(v['MANAGER_TELEGRAM_CHAT_ID'])
    # Use the real card renderer with staging DB, without any provider calls.
    os.environ['APP_ENV']='staging';os.environ['EXTERNAL_WRITES_ENABLED']='false'
    os.environ['DATABASE_URL']=dotenv_values('.env')['STAGING_DATABASE_URL']
    async def render():
        from app.services.desk_display import desk_card
        from app.database.session import engine
        try:return await desk_card(DRAFT)
        finally:await engine.dispose()
    body,keyboard=asyncio.run(render())
    has_claim=any(button.callback_data==f'desk:claim:{DRAFT}' for row in keyboard.inline_keyboard for button in row)
    assert has_claim or record['actor_telegram_id']==APPROVED_USER
    response=http.post('https://api.telegram.org/bot'+v['TELEGRAM_BOT_TOKEN']+'/editMessageText',json={
        'chat_id':state['group'],'message_id':original['telegram_message_id'],'text':body,
        'reply_markup':keyboard.model_dump(exclude_none=True)},timeout=25).json()
    unchanged=response.get('error_code')==400 and 'message is not modified' in response.get('description','').lower()
    assert response.get('ok') or unchanged
    emit('existing_card_ready',draft_id=DRAFT,telegram_message_id=original['telegram_message_id'],claim_button=has_claim,updated=bool(response.get('ok')))


def enable(state):
    draft(state)
    scoped=[r for r in queue() if r['draft_id']==DRAFT]
    assert all(r['destination']==(APPROVED_USER if r['direction']=='to_customer' else state['group']) for r in scoped)
    emit('exact_resume_scope_checked',draft_ids=[DRAFT],explicit_message_ids=[],existing_messages=[r['id'] for r in scoped])
    for role in ('manager','client'):
        v=rendered(state,role)
        assert v[SCOPE+'DRAFT_IDS']==str(DRAFT) and v[SCOPE+'MESSAGE_IDS']==''
        assert v[SCOPE+'CLIENT_RECIPIENT_IDS']==str(APPROVED_USER) and v[SCOPE+'MANAGER_CHAT_IDS']==str(state['group'])
        assert v['MANAGER_TELEGRAM_USER_IDS']==str(APPROVED_USER)
        change(state,role,{'ORDER_DESK_SEND_ENABLED':'true'})


if __name__=='__main__':
    try:
        parser=argparse.ArgumentParser();parser.add_argument('action',choices=('begin','link','card','enable'))
        args=parser.parse_args();{'begin':begin,'link':link,'card':card,'enable':enable}[args.action](load())
    except Exception as error:
        frame=traceback.extract_tb(error.__traceback__)[-1]
        emit('resume_failed',category=type(error).__name__,function=frame.name,line=frame.lineno)
        raise SystemExit(1)
