"""Sanitized error categories shared by runtime, smoke tests and settings."""
from google.auth.exceptions import RefreshError, TransportError
from sqlalchemy.exc import SQLAlchemyError
import requests
from googleapiclient.errors import HttpError


class EmailDatabaseUnavailable(RuntimeError):
    pass


def classify(error):
    if isinstance(error, RefreshError):
        # Inspect provider detail locally; never return/log its contents.
        return 'reauth_required' if 'invalid_grant' in str(error) else 'bad_credentials'
    if isinstance(error, (SQLAlchemyError, EmailDatabaseUnavailable)):
        return 'database_unavailable'
    if isinstance(error, HttpError):
        return 'bad_credentials' if error.resp.status in (401, 403) else 'network_unavailable'
    if isinstance(error, (ValueError, FileNotFoundError)):
        return 'bad_credentials'
    if isinstance(error, (TransportError, requests.RequestException, TimeoutError, ConnectionError, OSError)):
        return 'network_unavailable'
    if isinstance(error, RuntimeError) and 'authorization is required' in str(error):
        return 'reauth_required'
    return 'worker_error'


def retry_delay(status, failures, interval):
    if status in ('reauth_required', 'bad_credentials'):
        return max(interval, 900)
    return min(900, max(interval, 5) * 2 ** min(max(failures-1, 0), 5))
