"""Explicit post-consent token deployment to ONE named staging mailbox worker."""
import argparse
import json
import subprocess
import shutil
from pathlib import Path
from app.integrations.email.gmail_auth import validate_token_scopes
from app.integrations.email.mailboxes import mailbox
from scripts.configure_buying_staging import PROJECT, ENVIRONMENT


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mailbox',required=True,choices=['customer','supplier'])
    role=parser.parse_args().mailbox
    config=mailbox(role)
    cli=shutil.which('railway.cmd') or shutil.which('railway')
    if not cli:raise RuntimeError('Railway CLI is required')
    if not config.token_file:raise RuntimeError('Mailbox token file is required')
    token=Path(config.token_file).expanduser().resolve()
    if token.is_relative_to(Path.cwd().resolve()):raise RuntimeError('Token file must be outside repository')
    validate_token_scopes(json.loads(token.read_text(encoding='utf-8-sig')),config.scopes)
    if role=='customer':
        from app.integrations.email.gmail_provider import GmailEmailProvider
        provider=GmailEmailProvider()
        service='ohmysmell-email-worker-staging'
    else:
        from app.integrations.email.supplier_gmail import SupplierGmailProvider
        provider=SupplierGmailProvider()
        service='ohmysmell-supplier-email-worker-staging'
    provider.readonly_smoke_check(max_messages=1)
    scope=['--project',PROJECT,'--environment',ENVIRONMENT,'--service',service]
    result=subprocess.run([cli,'variable','list',*scope,'--json'],capture_output=True,text=True,timeout=45)
    if result.returncode:raise RuntimeError('Verify the separate staging service exists')
    remote=json.loads(result.stdout)
    if remote.get('APP_ENV')!='staging' or remote.get('EXTERNAL_WRITES_ENABLED','false').lower()!='false' or remote.get('SUPPLIER_EMAIL_SEND_ENABLED','false').lower()!='false':
        raise RuntimeError('Staging safety guard failed')
    raw=token.read_text(encoding='utf-8-sig')
    validate_token_scopes(json.loads(raw),config.scopes)
    result=subprocess.run([cli,'variable','set',config.prefix+'_TOKEN_JSON','--stdin',*scope,'--skip-deploys'],input=raw,capture_output=True,text=True,timeout=45)
    if result.returncode:raise RuntimeError('Token variable update failed')
    print(role.upper()+' token updated for its staging worker only; redeploy that worker; no email sent')


if __name__=='__main__':
    try:main()
    except Exception as error:
        from app.integrations.email.health import classify
        print('Staging token update failed: '+classify(error))
        raise SystemExit(1)
