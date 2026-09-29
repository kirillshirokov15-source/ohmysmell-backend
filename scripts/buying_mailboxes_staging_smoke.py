"""Verify the two safe Settings cards over staging HTTPS; no mailbox operations."""
import json
import os
from pathlib import Path
from dotenv import load_dotenv
from scripts.buying_staging_accounts import accounts
from scripts.buying_staging_e2e import BASE
from app.integrations.http_tls import verified_session


def main():
    load_dotenv()
    from app.bot.staging_runner import validate_staging_config
    validate_staging_config(dict(os.environ))
    with verified_session() as http:
        response=http.post(BASE+'/buying/auth/login',json=accounts()['manager'],timeout=(10,30))
        if response.status_code!=200:raise RuntimeError('Staging manager login failed')
        http.headers['Authorization']='Bearer '+response.json()['access_token']
        try:
            response=http.get(BASE+'/buying/settings/status',timeout=(10,30))
            if response.status_code!=200:raise RuntimeError('Staging Settings failed')
            data=response.json()
            assert 'gmail' not in data
            for key in ('customer_gmail','supplier_gmail'):
                assert set(data[key])=={'status','checked_at'}
                assert data[key]['status'] in ('connected','reauth_required','not_configured','error')
            assert data['supplier_email']['status']=='disabled'
            assert data['moysklad']['writes_enabled'] is False
            report={key:data[key] for key in ('customer_gmail','supplier_gmail')}
        finally:
            http.post(BASE+'/buying/auth/logout',timeout=(10,30))
    with verified_session() as http:
        response=http.get('https://ohmysmell-manager-bot-staging-staging.up.railway.app/ready',timeout=(10,30))
        report['manager_ready_http']=response.status_code
    Path('.staging-artifacts/mailboxes-http-smoke.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Staging mailbox Settings smoke failed: '+type(error).__name__)
        raise SystemExit(1)
