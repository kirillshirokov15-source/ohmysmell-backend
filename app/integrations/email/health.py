"""Sanitized error categories shared by runtime, smoke tests and settings."""
import json
from google.auth.exceptions import RefreshError, TransportError
from sqlalchemy.exc import SQLAlchemyError
import requests
from googleapiclient.errors import HttpError


class EmailDatabaseUnavailable(RuntimeError):
    pass


class MailboxNotConfigured(ValueError):
    pass


def classify(error):
    if isinstance(error, MailboxNotConfigured):
        return 'not_configured'
    if isinstance(error, RefreshError):
        # Inspect provider detail locally; never return/log its contents.
        return 'reauth_required' if 'invalid_grant' in str(error) else 'bad_credentials'
    if isinstance(error, (SQLAlchemyError, EmailDatabaseUnavailable)):
        return 'database_unavailable'
    if isinstance(error, HttpError):
        status = getattr(error.resp, "status", None)
        if status == 429:
            return "rate_limited"
        if status == 403:
            # A quota error is NOT invalid OAuth credentials. Do not request
            # reauthorization when Gmail simply wants fewer API calls.
            try:
                payload = json.loads(error.content)
                detail = payload.get("error", {})
                reasons = [entry.get("reason") for entry in detail.get("errors", [])
                           if isinstance(entry, dict)]
                if detail.get("reason") in {"rateLimitExceeded", "userRateLimitExceeded"} or any(
                    reason in {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}
                    for reason in reasons
                ):
                    return "rate_limited"
            except (ValueError, TypeError, AttributeError):
                pass
        return "bad_credentials" if status in (401, 403) else "network_unavailable"
    if isinstance(error, (ValueError, FileNotFoundError)):
        return 'bad_credentials'
    if isinstance(error, (TransportError, requests.RequestException, TimeoutError, ConnectionError, OSError)):
        return 'network_unavailable'
    if isinstance(error, RuntimeError) and 'authorization is required' in str(error):
        return 'reauth_required'
    return 'worker_error'


def retry_delay(status, failures, interval):
    if status in ('reauth_required', 'bad_credentials', 'not_configured'):
        return max(interval, 900)
    return min(900, max(interval, 5) * 2 ** min(max(failures-1, 0), 5))
