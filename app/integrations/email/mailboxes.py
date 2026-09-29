"""Mailbox boundaries. Legacy GMAIL_* aliases are CUSTOMER-only."""
from dataclasses import dataclass
import os
from pathlib import Path
from app.config.settings import settings

READONLY = 'https://www.googleapis.com/auth/gmail.readonly'
SEND = 'https://www.googleapis.com/auth/gmail.send'


@dataclass(frozen=True)
class Mailbox:
    role: str
    token_file: str
    credentials_file: str
    user_id: str
    scopes: tuple[str, ...]

    @property
    def prefix(self):
        return self.role.upper() + '_GMAIL'


def mailbox(role):
    if role == 'customer':
        result = Mailbox(role, settings.gmail_token_file, settings.gmail_credentials_file,
                         settings.gmail_user_id, (READONLY,))
    elif role == 'supplier':
        result = Mailbox(role, os.getenv('SUPPLIER_GMAIL_TOKEN_FILE', ''),
                         os.getenv('SUPPLIER_GMAIL_CREDENTIALS_FILE', ''),
                         os.getenv('SUPPLIER_GMAIL_USER_ID', 'me'), (READONLY, SEND))
    else:
        raise ValueError('Unknown Gmail mailbox role')
    paths = [settings.gmail_token_file, settings.gmail_credentials_file,
             os.getenv('SUPPLIER_GMAIL_TOKEN_FILE', ''), os.getenv('SUPPLIER_GMAIL_CREDENTIALS_FILE', '')]
    resolved = [Path(value).expanduser().resolve() for value in paths if value]
    if len(set(resolved)) != len(resolved):
        raise ValueError('Gmail token and client files must be distinct for each mailbox')
    return result


def expected_email(role):
    return os.getenv(role.upper() + '_GMAIL_EXPECTED_EMAIL', '').strip().casefold()


def verify_identity(role, profile):
    actual = profile.get('emailAddress', '').strip().casefold()
    expected, other = expected_email(role), expected_email('supplier' if role == 'customer' else 'customer')
    if not actual or (expected and actual != expected) or (other and actual == other):
        raise ValueError('Gmail account identity mismatch')
    if role == 'supplier' and (not expected or not other or expected == other):
        raise ValueError('Supplier Gmail requires two distinct EXPECTED_EMAIL identities')
    return actual


def configured(role):
    config = mailbox(role)
    return bool(os.getenv(config.prefix + '_TOKEN_JSON') or os.getenv(config.prefix + '_TOKEN_JSON_BASE64')
                or (config.token_file and Path(config.token_file).is_file())
                or (role == 'customer' and os.getenv('GMAIL_TOKEN_JSON_BASE64')))
