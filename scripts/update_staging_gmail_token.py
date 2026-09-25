"""Explicit post-consent deployment of a readonly token to the staging email worker."""
import base64
import json
import subprocess
import shutil
from pathlib import Path
from app.config.settings import settings
from app.integrations.email.gmail_auth import validate_token_scopes
from scripts.configure_buying_staging import PROJECT, ENVIRONMENT

SERVICE='56306c0e-ff3a-4b34-89e7-856c27d76da5'


def main():
    cli=shutil.which('railway.cmd') or shutil.which('railway')
    if not cli: raise RuntimeError('Railway CLI is required')
    token=Path(settings.gmail_token_file).expanduser().resolve()
    if token.is_relative_to(Path.cwd().resolve()):
        raise RuntimeError('Token file must be outside the repository')
    raw=token.read_bytes()
    validate_token_scopes(json.loads(raw.decode('utf-8-sig')))
    from app.integrations.email.gmail_provider import GmailEmailProvider
    GmailEmailProvider().readonly_smoke_check(max_messages=1)
    # Smoke may refresh the file. Read the current authorized value afterwards.
    raw=token.read_bytes()
    scope=['--project',PROJECT,'--environment',ENVIRONMENT,'--service',SERVICE]
    result=subprocess.run([cli,'variable','list',*scope,'--json'],capture_output=True,text=True,timeout=45)
    if result.returncode: raise RuntimeError('Cannot verify staging configuration')
    remote=json.loads(result.stdout)
    if remote.get('APP_ENV')!='staging' or remote.get('EXTERNAL_WRITES_ENABLED','false').lower()!='false':
        raise RuntimeError('Staging safety guard failed')
    if not remote.get('GMAIL_TOKEN_FILE','').startswith('/tmp/'):
        raise RuntimeError('Persistent token path requires explicit old-file replacement; see recovery docs')
    result=subprocess.run([cli,'variable','set','GMAIL_TOKEN_JSON_BASE64','--stdin',*scope,'--skip-deploys'],input=base64.b64encode(raw).decode(),capture_output=True,text=True,timeout=45)
    if result.returncode: raise RuntimeError('Token variable update failed')
    print('Readonly token updated for staging email service only. Redeploy that service; token not displayed.')


if __name__=='__main__':
    try: main()
    except Exception as error:
        from app.integrations.email.health import classify
        print('Staging token update failed: '+classify(error))
        raise SystemExit(1)
