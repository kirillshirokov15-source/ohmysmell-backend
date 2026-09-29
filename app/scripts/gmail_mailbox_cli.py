"""Explicit mailbox-specific consent and read-only smoke helpers."""
from app.integrations.email.secret_files import prepare_secret_files
from app.integrations.email.gmail_auth import load_gmail_credentials
from app.integrations.email.mailboxes import mailbox


def main(role, oauth=False):
    try:
        prepare_secret_files(role, overwrite_json=False)
        config = mailbox(role)
        if oauth:
            load_gmail_credentials(allow_interactive=True, mailbox_role=role)
        if role == 'customer':
            from app.integrations.email.gmail_provider import GmailEmailProvider
            provider = GmailEmailProvider()
        else:
            from app.integrations.email.supplier_gmail import SupplierGmailProvider
            provider = SupplierGmailProvider()
        result = provider.readonly_smoke_check()
        print(f'{role.upper()} Gmail: connected')
        print('Verified account: ' + result['account'])
        print('Readonly INBOX access: OK; no email sent')
        if role == 'supplier':
            print('gmail.send scope: present; sending was NOT tested')
    except Exception as error:
        from app.integrations.email.health import classify
        print(role.upper() + ' Gmail: ' + classify(error))
        print(f'Recovery: configure {role.upper()}_GMAIL_* and explicitly run app.scripts.{role}_gmail_oauth')
        raise SystemExit(1)
