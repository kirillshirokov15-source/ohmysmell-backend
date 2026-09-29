"""Rename existing staging Gmail secrets to CUSTOMER-only keys; never authorize."""
import base64
import json
import shutil
import subprocess
from scripts.configure_buying_staging import PROJECT, ENVIRONMENT
from app.integrations.email.gmail_auth import validate_token_scopes

SERVICE='56306c0e-ff3a-4b34-89e7-856c27d76da5'


def main():
    cli=shutil.which('railway.cmd') or shutil.which('railway')
    scope=['--project',PROJECT,'--environment',ENVIRONMENT,'--service',SERVICE]
    result=subprocess.run([cli,'variable','list',*scope,'--json'],capture_output=True,text=True,timeout=45)
    if result.returncode:raise RuntimeError('Cannot read staging customer configuration')
    existing=json.loads(result.stdout)
    if existing.get('APP_ENV')!='staging' or existing.get('EXTERNAL_WRITES_ENABLED','false').lower()!='false':
        raise RuntimeError('Staging safety guard failed')
    updates={}
    for kind in ('TOKEN','CREDENTIALS'):
        key='CUSTOMER_GMAIL_'+kind+'_JSON'
        if existing.get(key):continue
        encoded=existing.get('GMAIL_'+kind+'_JSON_BASE64')
        if not encoded:continue
        data=json.loads(base64.b64decode(encoded,validate=True).decode('utf-8-sig'))
        if kind=='TOKEN':validate_token_scopes(data)
        updates[key]=json.dumps(data)
        updates['CUSTOMER_GMAIL_'+kind+'_FILE']='/tmp/ohmysmell/customer/'+kind.lower()+'.json'
    updates.update(CUSTOMER_GMAIL_USER_ID=existing.get('GMAIL_USER_ID','me'),SUPPLIER_EMAIL_SEND_ENABLED='false',SUPPLIER_REPLIES_ENABLED='false')
    for key,value in updates.items():
        result=subprocess.run([cli,'variable','set',key,'--stdin',*scope,'--skip-deploys'],input=value,capture_output=True,text=True,timeout=45)
        if result.returncode:raise RuntimeError('Customer staging configuration update failed')
    print('Staging CUSTOMER Gmail keys configured; token/scopes unchanged; no supplier credentials copied')


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Staging customer configuration failed: '+type(error).__name__)
        raise SystemExit(1)
