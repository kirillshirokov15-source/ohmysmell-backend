"""Fail-closed staging release runner. Default: plan only; no provider mutations.

Execution additionally requires a separately approved exact clean commit and
four verified rollback images. A failed phase is never automatically retried.
"""
import argparse
import hashlib
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from scripts.staging_order_desk_audit import cli, PROJECT, ENVIRONMENT

TARGET = 'r71fd865eb98'
SERVICES = {
    'backend':'e3b0504a-672a-494e-8110-8769a996324d',
    'manager':'7aeac926-0277-4d2f-9765-89c3848858af',
    'client':'bdb9fb43-9446-491f-bea3-ee0879d9b831',
    'email':'56306c0e-ff3a-4b34-89e7-856c27d76da5'}
PHASES = ['validate_approval_and_artifacts','readonly_preflight','stop_producers_and_apps',
          'recheck_queues','fresh_backup','restore_local_verify','migrate',
          'deploy_backend','deploy_manager','restore_client','restore_email','health_readiness_smoke']


def verify_artifacts(report):
    if len(report)!=4 or len({r['service'] for r in report})!=4:
        raise ValueError('Four distinct rollback artifacts required')
    for r in report:
        if not (r.get('source_reproducible') or r.get('captured_source_verified')) or not r.get('image_rebuild_verified') or not r.get('deployment_id'):
            raise ValueError('Rollback image reproducibility not verified')
        archive=Path(r['captured_source_archive'])
        if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest()!=r['archive_sha256']:
            raise ValueError('Rollback source archive checksum mismatch')


def queues_clear(report):
    if report['legacy_unsent'] or report['buying_unnotified']:
        raise ValueError('Legacy/Buying notification backlog requires manual decision')
    if any(direction=='to_manager' and status!='sent' and n for direction,status,n in report['desk_queue']):
        raise ValueError('Manager queue must not be drained automatically')
    for service in report['services']:
        if service['name']=='Postgres': continue
        if service['flags']['APP_ENV']!='staging' or service['flags']['EXTERNAL_WRITES_ENABLED']!='false':
            raise ValueError('Unsafe staging flags')
        if service['name']=='ohmysmell-backend-staging' and service['flags']['TILDA_TEST_MODE']!='true':
            raise ValueError('Tilda staging test mode must remain enabled')
        if service['name']=='ohmysmell-client-bot-staging' and service['flags']['ORDER_DESK_SEND_ENABLED']!='false':
            raise ValueError('Client sending must remain disabled')


def verify_health_audit(report):
    if not report.get('all_services_use_staging_database') or not report.get('distinct_bot_ids'):
        raise ValueError('Service identity verification failed')
    if any(code!=200 for code in report['http'].values()):
        raise ValueError('Backend smoke failed')
    for role in ('manager','client'):
        service=report['services'][role]
        for path in ('/health','/ready'):
            check=service.get('container_http',{}).get(path,{})
            if check.get('status')!=200 or check.get('polling') is not True or check.get('external_writes') is not False:
                raise ValueError('Bot container readiness failed')
        identity=report['telegram'][role]
        if not identity.get('get_me_ok') or not identity.get('expected_username_matches') or identity.get('webhook_configured'):
            raise ValueError('Telegram identity/polling configuration failed')


def preflight():
    # Suppress the verbose report; the existing script saves redacted evidence.
    import contextlib,io
    from scripts.warehouse_release_preflight import main
    with contextlib.redirect_stdout(io.StringIO()): main()
    report=json.loads(Path('.staging-artifacts/warehouse-release-preflight.json').read_text())
    queues_clear(report)
    return report


def stage_url():
    from dotenv import dotenv_values
    from sqlalchemy.engine import make_url
    v=dotenv_values('.env')
    stage,prod=make_url(v['STAGING_DATABASE_URL']),make_url(v['DATABASE_URL'])
    variables=json.loads(cli('variable','list','--project',PROJECT,'--environment',ENVIRONMENT,
                            '--service','ba3da870-ebb7-4ef1-b352-f1a4293eb467','--json'))
    actual=make_url(variables['DATABASE_PUBLIC_URL'])
    identity=lambda u:(u.host,u.port or 5432,u.database,u.username,u.password)
    if identity(stage)!=identity(actual) or (stage.host,stage.port,stage.database)==(prod.host,prod.port,prod.database):
        raise ValueError('Staging endpoint identity mismatch')
    return stage


def wait_deployment(service, previous):
    for _ in range(120):
        rows=json.loads(cli('deployment','list','--service',service,'--environment',ENVIRONMENT,'--limit','1','--json'))
        if rows and rows[0]['id']!=previous:
            if rows[0]['status']=='SUCCESS': return rows[0]['id']
            if rows[0]['status'] in {'FAILED','CRASHED','REMOVED'}: raise ValueError('Deployment failed')
        time.sleep(3)
    raise ValueError('Deployment readiness deadline exceeded')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute',action='store_true')
    parser.add_argument('--approved-commit')
    args=parser.parse_args()
    commit=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
    plan={'project':PROJECT,'environment':ENVIRONMENT,'commit':commit,'migration':TARGET,'phases':PHASES}
    if not args.execute:
        print(json.dumps(plan,indent=2)); return
    if args.approved_commit!=commit or subprocess.check_output(['git','branch','--show-current'],text=True).strip()!='feature/sales-core-v2':
        raise ValueError('Separate approval for the exact release commit required')
    if subprocess.check_output(['git','status','--porcelain'],text=True).strip():
        raise ValueError('Release commit must be clean')
    rollback=json.loads(Path('.staging-artifacts/rollback-artifacts.json').read_text())
    verify_artifacts(rollback)
    root=Path('.staging-artifacts/releases')/('oms1-'+uuid4().hex)
    root.mkdir(parents=True)
    state={**plan,'phases_done':[],'started_at':datetime.now(timezone.utc).isoformat()}
    def phase(name, function):
        state['running']=name
        (root/'state.json').write_text(json.dumps(state,indent=2),encoding='utf-8')
        result=function()
        state['phases_done'].append(name)
        (root/'state.json').write_text(json.dumps(state,indent=2),encoding='utf-8')
        return result
    before=phase('readonly_preflight',preflight)
    if before['revision']!=['p59db643bc76']:
        raise ValueError('Unexpected initial database revision')
    for row in rollback:
        deployed=next(s for s in before['services'] if s['name']==row['service'])
        if deployed['deployment_id']!=row['deployment_id']:
            raise ValueError('Rollback deployment changed since verification')
    url=stage_url()
    # Prove the restore destination before taking any application offline.
    import psycopg2
    from scripts.restore_local_backup import verify_local
    with psycopg2.connect(host='127.0.0.1',port=55439,user='oms_test',
                          dbname='postgres',connect_timeout=10) as local:
        verify_local(local,'.staging-artifacts/acceptance-pg-20261010')
    def stop():
        for role in ('email','client','manager','backend'):
            cli('down','--project',PROJECT,'--environment',ENVIRONMENT,'--service',SERVICES[role],'--yes')
        # All worker deployments must finish removal, not merely accept down.
        for role in SERVICES:
            for _ in range(60):
                rows=json.loads(cli('deployment','list','--service',SERVICES[role],'--environment',ENVIRONMENT,'--limit','1','--json'))
                if rows and rows[0]['status']=='REMOVED': break
                time.sleep(2)
            else: raise ValueError('Worker still running')
    phase('stop_producers_and_apps',stop)
    stopped=phase('recheck_queues',preflight)
    if stopped['protected_hashes']!=before['protected_hashes']:
        raise ValueError('Protected order changed during maintenance entry')
    (root/'preflight-before.json').write_text(json.dumps(stopped),encoding='utf-8')
    def dump():
        from scripts.backup_restore_rehearsal import backup
        path=root/'staging-before-q60.dump'
        fp=backup(url,path)
        (root/'source-fingerprint.json').write_text(json.dumps(fp),encoding='utf-8')
        (root/'backup-report.json').write_text(json.dumps({'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}))
    phase('fresh_backup',dump)
    from scripts.restore_local_backup import restore
    phase('restore_local_verify',lambda:restore(root,'.staging-artifacts/acceptance-pg-20261010'))
    def migrate():
        from sqlalchemy import create_engine,text
        from alembic import command
        from alembic.config import Config
        engine=create_engine(url,connect_args={'connect_timeout':10})
        try:
            with engine.begin() as c:
                c.execute(text("SET LOCAL lock_timeout='5s'")); c.execute(text("SET LOCAL statement_timeout='60s'"))
                if c.scalar(text('SELECT version_num FROM alembic_version'))!='p59db643bc76':
                    raise ValueError('Revision changed before migration')
                cfg=Config('alembic.ini'); cfg.attributes['connection']=c
                command.upgrade(cfg,TARGET); command.check(cfg)
        finally: engine.dispose()
    phase('migrate',migrate)
    from scripts.staging_order_desk_deploy import prepare_upload
    upload=prepare_upload(commit,'backend')
    def deploy(role):
        old=next(s['deployment_id'] for s in before['services'] if s['id']==SERVICES[role])
        cli('up',str(upload),'--path-as-root','--no-gitignore','--project',PROJECT,'--environment',ENVIRONMENT,
            '--service',SERVICES[role],'--detach','--message','OhMySmell 1.0 '+commit)
        return wait_deployment(SERVICES[role],old)
    for role in ('backend','manager'): phase('deploy_'+role,lambda role=role:deploy(role))
    def resume(role):
        old=next(s['deployment_id'] for s in before['services'] if s['id']==SERVICES[role])
        rows=json.loads(cli('deployment','list','--service',SERVICES[role],'--environment',ENVIRONMENT,'--limit','1','--json'))
        if not rows or rows[0]['id']!=old: raise ValueError('Worker source changed; do not redeploy latest')
        cli('redeploy','--project',PROJECT,'--environment',ENVIRONMENT,'--service',SERVICES[role],'--yes')
        return wait_deployment(SERVICES[role],old)
    for role in ('client','email'): phase('restore_'+role,lambda role=role:resume(role))
    def health():
        import base64
        code="import os,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8082')+'/ready',timeout=10); assert r.status==200; print('OMS_EMAIL_READY')"
        encoded=base64.b64encode(code.encode()).decode()
        result=cli('ssh','--project',PROJECT,'--environment',ENVIRONMENT,'--service',SERVICES['email'],'--',
            "python -c 'import base64;exec(base64.b64decode(\""+encoded+"\"))'")
        if 'OMS_EMAIL_READY' not in result:
            raise ValueError('Customer email worker is not ready')
        from app.integrations.http_tls import verified_session
        with verified_session() as http:
            for role in ('backend','manager'):
                service=next(s for s in before['services'] if s['id']==SERVICES[role])
                for path in ('/health','/ready'):
                    response=http.get('https://'+service['domains'][0]+path,timeout=15)
                    response.raise_for_status()
                    if role=='manager' and response.json().get('polling') is not True:
                        raise ValueError('Manager not polling')
        after=preflight()
        if after['revision']!=[TARGET] or after['protected_hashes']!=before['protected_hashes']:
            raise ValueError('Post-release database invariant failed')
        if any(s['status']!='SUCCESS' for s in after['services']):
            raise ValueError('A staging service is not running successfully')
        for service in after['services']:
            prior=next(s for s in before['services'] if s['id']==service['id'])
            if service['flags']!=prior['flags'] or service['scope']!=prior['scope']:
                raise ValueError('Staging delivery restrictions changed')
        from scripts.staging_order_desk_audit import main as audit
        audit()  # read-only client container readiness, queues and public API
        verify_health_audit(json.loads(Path('.staging-artifacts/order-desk-live-audit.json').read_text()))
    phase('health_readiness_smoke',health)
    state['result']='passed'; (root/'state.json').write_text(json.dumps(state,indent=2))
    print(json.dumps({'result':'passed','report':str(root/'state.json')}))


if __name__=='__main__':
    try: main()
    except Exception as error:
        print(json.dumps({'result':'STOP','error_type':type(error).__name__}))
        raise SystemExit(1)
