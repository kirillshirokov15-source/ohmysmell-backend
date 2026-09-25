"""Provision staging-only Buying credentials without exposing them in output/argv."""
import json
import shutil
import subprocess
import secrets
from dotenv import dotenv_values, set_key

PROJECT='142df0b6-cee3-41a8-9353-ea14ec15f0b0'
SERVICE='e3b0504a-672a-494e-8110-8769a996324d'
ENVIRONMENT='fa04fc09-13d2-4bc4-8f99-486422c7fcb7'


def main():
    cli=shutil.which('railway.cmd') or shutil.which('railway')
    if not cli:
        raise RuntimeError('Railway CLI is required')
    scope=['--project',PROJECT,'--environment',ENVIRONMENT,'--service',SERVICE]
    result=subprocess.run([cli,'variable','list',*scope,'--json'],capture_output=True,text=True,timeout=45)
    if result.returncode:
        raise RuntimeError('Cannot read staging configuration')
    remote=json.loads(result.stdout)
    if remote.get('APP_ENV')!='staging' or remote.get('EXTERNAL_WRITES_ENABLED','false').lower()!='false':
        raise RuntimeError('Staging safety guard failed')
    local=dotenv_values('.env')
    for key in ('BUYING_SESSION_SECRET',):
        if key in remote and remote[key] is None:
            raise RuntimeError('Existing sealed credential must be preserved; configure local acceptance separately')
        value=remote.get(key) or local.get(key) or secrets.token_urlsafe(32)
        if not remote.get(key):
            result=subprocess.run([cli,'variable','set',key,'--stdin',*scope,'--skip-deploys'],input=value,capture_output=True,text=True,timeout=45)
            if result.returncode:
                raise RuntimeError('Staging credential configuration failed')
        set_key('.env',key,value)
    result=subprocess.run([cli,'variable','set','SUPPLIER_EMAIL_SEND_ENABLED=false',*scope,'--skip-deploys'],capture_output=True,text=True,timeout=45)
    if result.returncode:
        raise RuntimeError('Staging send guard configuration failed')
    result=subprocess.run([cli,'variable','set','BUYING_ALLOWED_ORIGINS=https://ohmysmell-buying.christiankvyatkovsky.chatgpt.site',*scope,'--skip-deploys'],capture_output=True,text=True,timeout=45)
    if result.returncode:
        raise RuntimeError('Staging Sites origin configuration failed')
    print('Buying staging credentials configured; local .env updated; values not displayed')


if __name__=='__main__':
    try:
        main()
    except Exception as error:
        print('Buying staging configuration failed: '+type(error).__name__)
        raise SystemExit(1)
